from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from django.db import transaction
from django.utils import timezone
from scheduling.models import Nurse, ShiftType, ShiftRequest, ScheduleDraft, ScheduleDraftVersion, SchedulePublication, BedPublication
from scheduling.services.common import BusinessError, require_revision, lock_group, audit, notify, dates, parse_date

CYCLE = ['A','N','POST','OFF']


def effective_shifts(group, day):
    result = {}
    for shift in ShiftType.objects.filter(group=group, effective_from__lte=day).order_by('effective_from','id'):
        result[shift.code] = shift
    return result


def current_publication(group, day):
    # A new publication replaces only the dates it covers, including the
    # second week of a fourteen-day publication. Never fall back to an older
    # version merely because the latest version has cancellation overlays.
    candidates=SchedulePublication.objects.filter(group=group, start_date__lte=day,
        start_date__gte=day-timedelta(days=13)).order_by('-id')
    return next((pub for pub in candidates if (day-pub.start_date).days < pub.duration_days), None)


def published_entries(group, day):
    pub = current_publication(group, day)
    return effective_publication_entries(pub,day) if pub else []


def effective_publication_entries(pub,day=None):
    """Apply cancellation overlays while retaining the immutable original snapshot."""
    from scheduling.cancellation_models import ScheduleCancellationEntry
    query=ScheduleCancellationEntry.objects.filter(publication=pub)
    if day is not None: query=query.filter(date=day)
    revoked={(row[0],str(row[1]),row[2]) for row in query.values_list('nurse_id_at_action','date','shift')}
    return [e for e in pub.snapshot['entries'] if (day is None or e['date']==str(day))
            and (e['nurse'],e['date'],e['shift']) not in revoked]


def publication_status(pub):
    original=pub.snapshot.get('entries',[]);uncancelled=effective_publication_entries(pub)
    current_days={str(day) for day in dates(pub.start_date,pub.duration_days)
                  if (latest:=current_publication(pub.group,day)) and latest.pk==pub.pk}
    effective=[entry for entry in uncancelled if entry['date'] in current_days]
    if not current_days: status='replaced'
    elif not effective: status='cancelled'
    elif len(current_days)<pub.duration_days: status='partially_replaced'
    elif len(effective)<len(original): status='partially_cancelled'
    else: status='published'
    return {'status':status,'original_records':len(original),'effective_records':len(effective),
            'cancelled_records':len(original)-len(uncancelled),
            'replaced_records':sum(entry['date'] not in current_days for entry in uncancelled),
            'effective_dates':sorted({e['date'] for e in effective})}


def shift_phase(group,entry,now=None):
    """Conservative timing: the system has no actual attendance source."""
    now=now or timezone.localtime();day=parse_date(entry['date'])
    shift=effective_shifts(group,day).get(entry['shift'])
    if not shift:
        return 'future' if day>now.date() else 'started_unknown'
    if not shift.working:
        return 'future' if day>now.date() else 'started'
    zone=ZoneInfo('Asia/Shanghai')
    start=datetime.combine(day,shift.start,tzinfo=zone)
    end=datetime.combine(day+timedelta(days=int(shift.next_day)),shift.end,tzinfo=zone)
    if now<start:return 'future'
    if end<=start:return 'started_unknown'
    return 'in_progress' if now<end else 'started'


def historical_changes(draft):
    """Identify proposed changes to started published shifts, without claiming attendance."""
    changes=[];now=timezone.localtime()
    for day in dates(draft.start_date,draft.duration_days):
        pub=current_publication(draft.group,day)
        if not pub:continue
        old={e['nurse']:e for e in effective_publication_entries(pub,day)}
        new={e['nurse']:e for e in draft.entries if e['date']==str(day)}
        original={e['nurse']:e for e in pub.snapshot.get('entries',[]) if e.get('date')==str(day)}
        for nurse in set(old)|set(new):
            before=old.get(nurse);after=new.get(nurse)
            if (before or {}).get('shift')==(after or {}).get('shift'):continue
            references=[entry for entry in (before,original.get(nurse),after) if entry]
            if references and any(shift_phase(draft.group,entry,now)!='future' for entry in references):
                changes.append({'date':str(day),'nurse':nurse,'before':before.get('shift') if before else None,
                                'after':after.get('shift') if after else None,'publication':pub.id})
    return changes


def base_shift(nurse, day, rules):
    if nurse.status != 'active' or day < nurse.effective_from: return 'OFF'
    if nurse.base_type == 'day': return 'DAY' if day.weekday() in nurse.workdays else 'OFF'
    if nurse.subgroup is None: return 'OFF'
    return CYCLE[((day - parse_date(rules['anchor'])).days + rules['phases'][nurse.subgroup-1]) % 4]


def draft_data(draft):
    generated=draft.generation_versions.filter(revision__lte=draft.revision).order_by('-revision').first()
    return {'id':draft.id,'group':draft.group_id,'start_date':str(draft.start_date),
            'end_date':str(draft.start_date+timedelta(days=draft.duration_days-1)), 'duration_days':draft.duration_days, 'entries':draft.entries,
            'request_review_required':isinstance(draft.request_snapshot,dict) and draft.request_snapshot.get('stale') is True,
            'revision':draft.revision,'generation_version_id':generated.id if generated else None,
            'generation_summary':generated.summary if generated else None,'updated_at':draft.updated_at.isoformat()}


@transaction.atomic
def generate(group, user, start, include_requests=True, rotation_only=False, revision=None, duration_days=7):
    from scheduling.services import shift_requests
    lock_group(group)
    start = parse_date(start)
    if start.weekday()!=0: raise BusinessError('排班周期请从周一开始。')
    if type(duration_days) is not int or duration_days not in (7,14): raise BusinessError('排班周期只能为7天或14天。')
    if type(include_requests) is not bool or type(rotation_only) is not bool: raise BusinessError('排班生成选项无效。')
    end=start+timedelta(days=duration_days-1)
    draft = ScheduleDraft.objects.filter(group=group,start_date=start).first()
    if draft: require_revision(draft, revision)
    nurses = list(Nurse.objects.filter(group=group).exclude(status='left'))
    if not any(n.status=='active' and (not rotation_only or n.base_type=='rotation') for n in nurses):
        raise BusinessError('当前范围没有可参与排班的在岗护士，未生成草稿。',422)
    entries = []
    for nurse in nurses:
        if rotation_only and nurse.base_type=='day': continue
        for day in dates(start,duration_days):
            code = base_shift(nurse,day,group.rules)
            entries.append({'nurse':nurse.id,'date':str(day),'shift':code,'original':code,'source':'automatic','reason':''})
    if include_requests:
        requests = shift_requests.effective_requests(group,start,end)
        index = {(e['nurse'],e['date']):e for e in entries}
        used=set()
        for request in requests:
            participants=[request.nurse_id]+([request.partner_id] if request.kind=='swap' and request.partner_id else [])
            keys={(nurse_id,request.date) for nurse_id in participants}
            if used.intersection(keys): raise BusinessError('同一护士在同一天存在冲突的已批准申请，请先处理。',422)
            used.update(keys)
            entry = index.get((request.nurse_id,str(request.date)))
            if not entry: continue
            old = entry['shift']
            if request.kind=='swap':
                partner=index.get((request.partner_id,str(request.date)))
                if not request.partner_consented or not partner or partner['shift']!=request.target_shift:
                    raise BusinessError('换班申请与目标护士实际班次不一致，请重新核对申请。',422)
            entry.update(shift=request.target_shift,source='approved_request',request_id=request.id,
                         request_version=request.version_number,request_revision=request.revision,reason=request.reason)
            if request.kind=='swap' and request.partner_id:
                partner = index.get((request.partner_id,str(request.date)))
                if partner: partner.update(shift=old,source='approved_request',request_id=request.id,
                    request_version=request.version_number,request_revision=request.revision,reason=request.reason)
    # A cancellation belongs to the earlier formal version or draft revision.
    # An explicitly generated new draft is not masked by its old cell holds.
    if not entries: raise BusinessError('没有生成任何班次记录，草稿未保存。',422)
    keys={(entry['nurse'],entry['date']) for entry in entries}
    if len(keys)!=len(entries): raise BusinessError('生成结果包含重复排班格，草稿未保存。',422)
    working=0;shifts_by_day={str(day):effective_shifts(group,day) for day in dates(start,duration_days)}
    for entry in entries:
        shift=shifts_by_day[entry['date']].get(entry['shift'])
        if shift and shift.working: working+=1
    request_snapshot=shift_requests.json_snapshot(group,start,end)
    summary={'days':duration_days,'end_date':str(end),'nurses':len({e['nurse'] for e in entries}),
             'generated_cells':len(entries),'saved_cells':len(entries),'working_shifts':working,
             'rotation_only':rotation_only,'include_requests':include_requests,
             'request_snapshot':request_snapshot,'rules':group.rules}
    if draft:
        audit(user,group,'重新生成排班草稿',{'draft':draft.id,'previous_revision':draft.revision,'previous_entries':draft.entries})
        draft.entries=entries;draft.duration_days=duration_days;draft.request_snapshot=request_snapshot;draft.revision+=1;draft.save()
    else: draft=ScheduleDraft.objects.create(group=group,start_date=start,duration_days=duration_days,
                                             request_snapshot=request_snapshot,entries=entries)
    version=ScheduleDraftVersion.objects.create(draft=draft,revision=draft.revision,entries=entries,summary=summary,created_by=user)
    audit(user,group,'生成排班',{'draft':draft.id,'revision':draft.revision,'generation_version':version.id,'summary':summary})
    return draft


def validate(draft):
    from scheduling.services import shift_requests
    group=draft.group; nurses={n.id:n for n in Nurse.objects.filter(group=group)}
    errors=[]; warnings=[]; entries=draft.entries; index={}; counts={}
    # A formal overview can validate hundreds of cells on every refresh.
    # Effective shift definitions are identical for the same day in this read.
    shift_cache={}
    def shifts(day):
        if day not in shift_cache: shift_cache[day]=effective_shifts(group,day)
        return shift_cache[day]
    def err(message, entry=None, cross_week=False):
        issue={'message':message, **({k:entry[k] for k in ['nurse','date'] if k in entry} if entry else {})}
        if draft.duration_days==14:
            issue['period']='cross_week' if cross_week else ('general' if not entry or not entry.get('date') else
                'week_2' if entry['date']>=str(draft.start_date+timedelta(days=7)) else 'week_1')
        errors.append(issue)
    for entry in entries:
        day=parse_date(entry['date']); key=(entry['nurse'],entry['date']); nurse=nurses.get(entry['nurse'])
        if not draft.start_date<=day<=draft.start_date+timedelta(days=draft.duration_days-1): err('排班格日期超出草稿周期',entry)
        if key in index: err('同一天重复排班',entry)
        index[key]=entry
        shift=shifts(day).get(entry['shift'])
        counts.setdefault(entry['date'],{}); counts[entry['date']][entry['shift']]=counts[entry['date']].get(entry['shift'],0)+1
        if not nurse or not shift or not shift.enabled: err('人员或班次无效',entry); continue
        if shift.working and (nurse.status!='active' or day<nurse.effective_from): err(nurse.name+' 不在可排班状态',entry)
        if shift.working and entry['shift'] not in nurse.qualifications and entry['shift'] in ['A','DAY','N','TRAIN']:
            err(nurse.name+' 缺少该班次资格',entry)
    for day in dates(draft.start_date,draft.duration_days):
        for code, minimum in group.rules['min_staff'].items():
            if counts.get(str(day),{}).get(code,0)<minimum: err(str(day)+' '+code+'班人数不足（最低'+str(minimum)+'人）',{'date':str(day)})
        for nurse in nurses.values():
            if nurse.status=='active' and (nurse.id,str(day)) not in index:
                err(nurse.name+' '+str(day)+' 缺少班次',{'nurse':nurse.id,'date':str(day)})
    # Context includes both adjacent publications, so a week boundary cannot hide night/rest conflicts.
    context={}
    for day in dates(draft.start_date-timedelta(days=7),draft.duration_days+14):
        for e in published_entries(group,day): context[(e['nurse'],e['date'])]=e
    context.update(index)
    for nurse in nurses.values():
        if nurse.status=='active' and nurse.base_type=='rotation' and nurse.subgroup is None:
            err(nurse.name+' 尚未分配轮转小组，不能发布排班')
        work=[]; consecutive=0
        for day in dates(draft.start_date-timedelta(days=7),draft.duration_days+14):
            entry=context.get((nurse.id,str(day))); shift=shifts(day).get(entry['shift']) if entry else None
            if shift and shift.working:
                consecutive+=1
                start=datetime.combine(day,shift.start,tzinfo=ZoneInfo('Asia/Shanghai'))
                end=datetime.combine(day+timedelta(days=int(shift.next_day)),shift.end,tzinfo=ZoneInfo('Asia/Shanghai'))
                if work:
                    prev=work[-1]; gap=(start-prev[1]).total_seconds()/3600
                    if gap<group.rules['min_rest_hours'] and (str(day) in counts or prev[2] in counts):
                        err(nurse.name+' 班次重叠或休息不足',entry,
                            draft.duration_days==14 and prev[2]<str(draft.start_date+timedelta(days=7))<=str(day))
                work.append((start,end,str(day)))
                if consecutive>group.rules['max_consecutive_days'] and str(day) in counts: err(nurse.name+' 连续工作天数超限',entry)
            else: consecutive=0
            prev=context.get((nurse.id,str(day-timedelta(days=1))))
            if prev and prev['shift']=='N' and entry and entry['shift'] not in ['POST','OFF'] and (str(day) in counts or str(day-timedelta(days=1)) in counts):
                err(nurse.name+' N班后必须夜下休或休',entry,
                    draft.duration_days==14 and day==draft.start_date+timedelta(days=7))
    end=draft.start_date+timedelta(days=draft.duration_days-1)
    if draft.request_snapshot is not None and draft.request_snapshot!=shift_requests.json_snapshot(group,draft.start_date,end):
        err('排班生成后调班申请发生变化，请重新生成并校验草稿。')
    active_requests=list(shift_requests.effective_requests(group,draft.start_date,end))
    active_ids={item.id for item in active_requests}
    for entry in entries:
        if entry.get('source')=='approved_request' and entry.get('request_id') not in active_ids:
            err('排班中引用的调班申请已撤销、替代或失效，请重新生成或修改。',entry)
    for request in active_requests:
        e=index.get((request.nurse_id,str(request.date)))
        if not e or e.get('request_id')!=request.id or e['shift']!=request.target_shift:
            err('已批准申请尚未应用：'+request.nurse.name, {'nurse':request.nurse_id,'date':str(request.date)})
        if request.kind=='swap':
            partner=index.get((request.partner_id,str(request.date)))
            if not request.partner_consented or not partner or partner.get('request_id')!=request.id:
                err('换班对方尚未同意或未应用',{'nurse':request.partner_id,'date':str(request.date)})
    if not group.rules.get('clinical_thresholds_confirmed'):
        warnings.append({'message':'当前休息间隔、人数及护理负荷阈值为演示配置，需科室确认。'})
    for nurse in nurses.values():
        if nurse.special_restrictions and any(e['nurse']==nurse.id and e['shift'] not in ['OFF','POST'] for e in entries):
            warnings.append({'message':nurse.name+' 存在人工记录的特殊排班限制，请逐项核对：'+nurse.special_restrictions[:150]})
    changes=historical_changes(draft)
    return {'valid':not errors,'errors':errors,'warnings':warnings,'counts':counts,'revision':draft.revision,
            'historical_correction_count':len(changes),'historical_corrections':changes}


@transaction.atomic
def edit(draft, user, data):
    lock_group(draft.group); draft.refresh_from_db(); require_revision(draft,data.get('revision'))
    entry=next((e for e in draft.entries if e['nurse']==data.get('nurse') and e['date']==data.get('date')),None)
    created=False
    day=parse_date(data.get('date'))
    if not draft.start_date<=day<=draft.start_date+timedelta(days=draft.duration_days-1): raise BusinessError('日期不在当前排班周期内。')
    if not entry:
        from scheduling.cancellation_models import ScheduleCancellationHold
        nurse=Nurse.objects.filter(group=draft.group,pk=data.get('nurse')).first()
        if not nurse or not ScheduleCancellationHold.objects.filter(group=draft.group,nurse_id_at_action=nurse.id,date=day,active=True).exists():
            raise BusinessError('未找到目标排班格。')
        original=base_shift(nurse,day,draft.group.rules)
        entry={'nurse':nurse.id,'date':str(day),'shift':original,'original':original,'source':'automatic','reason':''}
        draft.entries.append(entry)
        created=True
    shift=effective_shifts(draft.group,day).get(data.get('shift'))
    if not shift or not shift.enabled: raise BusinessError('班次无效或未启用。')
    reason=str(data.get('reason','')).strip()
    if not reason: raise BusinessError('请填写调整原因。')
    old=None if created else dict(entry); entry.update(shift=shift.code,source='manager_edit',reason=reason)
    for key in ('request_id','request_version','request_revision'):entry.pop(key,None)
    from scheduling.cancellation_models import ScheduleCancellationHold
    from django.utils import timezone
    ScheduleCancellationHold.objects.filter(group=draft.group,nurse_id_at_action=entry['nurse'],date=day,active=True).update(active=False,resolved_at=timezone.now())
    draft.revision+=1; draft.save()
    audit(user,draft.group,'人工修改班次',{'before':old,'after':entry,'revision':draft.revision})
    return draft


@transaction.atomic
def publish(draft,user,data):
    lock_group(draft.group); draft.refresh_from_db(); require_revision(draft,data.get('revision'))
    existing=SchedulePublication.objects.filter(draft=draft,draft_revision=draft.revision).first()
    if existing:
        if publication_status(existing)['status']!='published':
            raise BusinessError('该草稿修订对应的正式版本已撤销或被替代，请重新生成或编辑草稿后再发布。',409)
        return existing
    report=validate(draft)
    if not report['valid']: raise BusinessError('排班存在阻断性异常，不能发布。',422,report['errors'])
    history=historical_changes(draft)
    if history:
        reason=data.get('correction_reason','')
        if data.get('historical_correction') is not True or not isinstance(reason,str) or not 1<=len(reason.strip())<=500:
            raise BusinessError(f'本次包含 {len(history)} 条已开始或过去的正式排班更正；请使用授权历史更正并填写原因。',422)
    # A completed historical bed assignment remains an immutable fact linked to
    # its original schedule. Only an explicitly authorized historical correction
    # may supersede that schedule; future or in-progress assignments still block.
    preserved_historical_beds=[]
    for bp in BedPublication.objects.filter(group=draft.group,date__range=[draft.start_date,draft.start_date+timedelta(days=draft.duration_days-1)]).order_by('-id'):
        latest=BedPublication.objects.filter(group=draft.group,date=bp.date,shift=bp.shift).order_by('-id').first()
        if latest.pk!=bp.pk: continue
        allowed={e['nurse'] for e in draft.entries if e['date']==str(bp.date) and e['shift']==bp.shift}
        if any(e['nurse'] not in allowed for e in bp.snapshot['assignments']):
            phase=shift_phase(draft.group,{'date':str(bp.date),'shift':bp.shift})
            if history and data.get('historical_correction') is True and phase=='started':
                preserved_historical_beds.append(bp.id)
            else:
                raise BusinessError('本次排班修订影响尚未结束或时间不明的已发布床位责任，请先完成安全交接。',422)
    previous=current_publication(draft.group,draft.start_date)
    generation=draft.generation_versions.filter(revision__lte=draft.revision).order_by('-revision').first()
    nurses={n.id:n for n in Nurse.objects.filter(group=draft.group)}; entries=[]
    for e in draft.entries:
        n=nurses[e['nurse']]; s=effective_shifts(draft.group,parse_date(e['date']))[e['shift']]
        entries.append({**e,'name':n.name,'level':n.level,'subgroup':n.subgroup,'base_type':n.base_type,'is_leader':n.is_leader,
                        'hours':float(s.hours),'working':s.working,'shift_name':s.name})
    pub=SchedulePublication.objects.create(group=draft.group,draft=draft,draft_revision=draft.revision,start_date=draft.start_date,duration_days=draft.duration_days,
        snapshot={'entries':entries,'rules':draft.group.rules,'validation':report,
            'request_snapshot':draft.request_snapshot,'duration_days':draft.duration_days,
            'previous_publication':previous.id if previous else None,'generation_version':generation.id if generation else None},published_by=user)
    audit(user,draft.group,'发布排班',{'publication':pub.id,'draft':draft.id,'revision':draft.revision,
        'historical_correction':bool(history),'correction_reason':data.get('correction_reason','').strip() if history else '',
        'historical_changes':history,'preserved_historical_bed_publications':preserved_historical_beds,
        'previous_publication':previous.id if previous else None,'generation_version':generation.id if generation else None})
    notify(draft.group,'已发布 '+str(draft.start_date)+' 起的排班，版本 #'+str(pub.id))
    return pub
