"""Preview and cancel selected schedule cells without rewriting publications."""
import hashlib
import json
from collections import Counter
from datetime import timedelta

from django.db import transaction
from django.db.models import Q, F
from django.utils import timezone

from scheduling.cancellation_models import ScheduleCancellation, ScheduleCancellationEntry, ScheduleCancellationHold
from scheduling.models import Nurse, ScheduleDraft, SchedulePublication, ShiftRequest, ShiftType, BedDraft, BedPublication
from scheduling.services.common import BusinessError, audit, lock_group, parse_date
from scheduling.services import schedule


def _criteria(group,data):
    start=parse_date(data.get('start_date')); end=parse_date(data.get('end_date'))
    if end<start: raise BusinessError('开始日期不能晚于结束日期。')
    if (end-start).days>92: raise BusinessError('单次撤销范围不能超过93天。')
    scope=data.get('nurse_scope','all'); shift=data.get('shift','ALL'); subgroup=data.get('subgroup')
    if scope not in ['all','rotation','day','subgroup']: raise BusinessError('人员范围无效。')
    if scope=='subgroup' and (type(subgroup) is not int or not 1<=subgroup<=len(group.rules.get('phases',[]))):
        raise BusinessError('请选择有效的小组。')
    if not isinstance(shift,str) or (shift!='ALL' and not ShiftType.objects.filter(group=group,code=shift).exists()):
        raise BusinessError('所选班次无效。')
    ids=data.get('nurse_ids',[])
    if not isinstance(ids,list) or any(type(v) is not int or v<1 for v in ids) or len(set(ids))!=len(ids):
        raise BusinessError('指定护士范围无效。')
    formal_only=data.get('formal_only',False);publication_id=data.get('publication_id')
    correction=data.get('historical_correction',False)
    if type(formal_only) is not bool or type(correction) is not bool:raise BusinessError('撤销模式无效。')
    if formal_only and (type(publication_id) is not int or publication_id<1):raise BusinessError('请选择正式排班版本。')
    query=Nurse.objects.filter(group=group)
    if scope in ['rotation','day']: query=query.filter(base_type=scope)
    if scope=='subgroup': query=query.filter(subgroup=subgroup)
    if ids:
        query=query.filter(id__in=ids)
    nurses=list(query.order_by('id').values('id','name','employee_id','base_type','subgroup','revision'))
    return {'start_date':str(start),'end_date':str(end),'nurse_scope':scope,
            'subgroup':subgroup if scope=='subgroup' else None,'shift':shift,'nurse_ids':sorted(ids),
            'formal_only':formal_only,'publication_id':publication_id if formal_only else None,
            'historical_correction':correction},nurses,start,end


def _key(entry): return (int(entry['nurse']),entry['date'],entry['shift'])


def preview(group,data):
    criteria,nurses,start,end=_criteria(group,data); allowed={n['id'] for n in nurses}; shift=criteria['shift']
    publications=list(SchedulePublication.objects.filter(group=group,start_date__lte=end,
        start_date__gte=start-timedelta(days=13)).order_by('id'))
    current_ids=set(Nurse.objects.filter(group=group).values_list('id',flat=True))
    known_ids=set(current_ids)
    known_ids.update(e.get('nurse') for p in publications for e in p.snapshot.get('entries',[]) if e.get('nurse') is not None)
    if not set(criteria['nurse_ids']).issubset(known_ids):raise BusinessError('指定护士不属于当前大组或该范围的正式记录。',403)
    latest={}
    for pub in publications:
        day=max(start,pub.start_date)
        while day<=min(end,pub.start_date+timedelta(days=pub.duration_days-1)):
            latest[str(day)]=pub; day+=timedelta(days=1)
    if criteria['formal_only']:
        target=next((p for p in publications if p.id==criteria['publication_id']),None)
        if not target or start<target.start_date or end>target.start_date+timedelta(days=target.duration_days-1):
            raise BusinessError('所选范围不属于指定正式排班版本。',409)
        if any(latest.get(str(day))!=target for day in schedule.dates(start,(end-start).days+1)):
            raise BusinessError('正式排班版本已变化，请刷新并重新预览。',409)
    selected_published=[]
    for pub in publications:
        for entry in schedule.effective_publication_entries(pub):
            is_historical=entry['nurse'] not in current_ids
            historical_allowed=(is_historical and (not criteria['nurse_ids'] or entry['nurse'] in criteria['nurse_ids']) and
                (criteria['nurse_scope']=='all' or criteria['nurse_scope'] in ('rotation','day') and entry.get('base_type')==criteria['nurse_scope'] or
                 criteria['nurse_scope']=='subgroup' and entry.get('subgroup')==criteria['subgroup']))
            if (start.isoformat()<=entry['date']<=end.isoformat() and latest.get(entry['date'])==pub
                    and (not criteria['formal_only'] or pub.id==criteria['publication_id'])
                    and (entry['nurse'] in allowed or historical_allowed) and (shift=='ALL' or entry['shift']==shift)):
                selected_published.append({'publication':pub.id,'entry':entry})
    drafts=[] if criteria['formal_only'] else list(ScheduleDraft.objects.filter(group=group,start_date__lte=end,
        start_date__gte=start-timedelta(days=13)).order_by('id'))
    selected_drafts=[]
    for draft in drafts:
        for entry in draft.entries:
            if (start.isoformat()<=entry['date']<=end.isoformat() and entry['nurse'] in allowed
                    and (shift=='ALL' or entry['shift']==shift)):
                selected_drafts.append({'draft':draft.id,'revision':draft.revision,'entry':entry})
    cells={(row['entry']['nurse'],row['entry']['date']) for row in selected_published+selected_drafts}
    request_ids=set()
    if cells:
        for req in ShiftRequest.objects.filter(group=group,date__range=(start,end)).filter(Q(nurse_id__in=allowed)|Q(partner_id__in=allowed)):
            if (req.nurse_id,str(req.date)) in cells or (req.partner_id,str(req.date)) in cells: request_ids.add(req.id)
    now=timezone.localtime();protected=[];affected_shifts=set()
    for row in selected_published:
        entry=row['entry'];phase=schedule.shift_phase(group,entry,now)
        affected_shifts.add((entry['date'],entry['shift']))
        if phase!='future':protected.append({'nurse':entry['nurse'],'date':entry['date'],'shift':entry['shift'],'phase':phase})
    bed_versions={}
    for bedpub in BedPublication.objects.filter(group=group,date__range=(start,end)).order_by('id'):
        bed_versions[(str(bedpub.date),bedpub.shift)]=bedpub
    related_bed=[bed_versions[key] for key in affected_shifts if key in bed_versions]
    active_conflicts=[]
    for row in protected:
        bedpub=bed_versions.get((row['date'],row['shift']))
        if row['phase'] in ('in_progress','started_unknown') and bedpub and any(a.get('nurse')==row['nurse'] for a in bedpub.snapshot.get('assignments',[])):
            active_conflicts.append({'date':row['date'],'shift':row['shift'],'bed_publication':bedpub.id,'nurse':row['nurse']})
    related_drafts=[{'id':d.id,'revision':d.revision,'schedule_publication':d.schedule_publication_id}
        for d in BedDraft.objects.filter(group=group,date__range=(start,end)) if (str(d.date),d.shift) in affected_shifts]
    snapshot={'criteria':criteria,'nurses':nurses,
        'published':sorted(selected_published,key=lambda r:(r['publication'],_key(r['entry']))),
        'drafts':sorted(selected_drafts,key=lambda r:(r['draft'],_key(r['entry']))),
        'requests':sorted(request_ids),'protected':protected,'active_conflicts':active_conflicts,
        'bed_publications':sorted(p.id for p in related_bed),'bed_drafts':sorted(related_drafts,key=lambda d:d['id'])}
    fingerprint=hashlib.sha256(json.dumps(snapshot,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
    members={n['id']:n for n in nurses if n['id'] in {i for i,_ in cells}}
    for row in selected_published:
        entry=row['entry'];members.setdefault(entry['nurse'],{'id':entry['nurse'],'name':entry.get('name','历史护士'),
            'employee_id':'历史档案','base_type':entry.get('base_type'),'subgroup':entry.get('subgroup')})
    return {'criteria':criteria,'fingerprint':fingerprint,'affected_dates':len({d for _,d in cells}),
            'affected_nurses':len({n for n,_ in cells}),'affected_cells':len(cells),
            'published_records':len(selected_published),'draft_records':len(selected_drafts),
            'related_requests':len(request_ids),'affected_members':list(members.values()),
            'protected_records':len(protected),'active_bed_conflicts':len(active_conflicts),
            'related_bed_publications':len(related_bed),'related_bed_drafts':len(related_drafts),
            'affected_shift_counts':dict(Counter(row['entry']['shift'] for row in selected_published)),
            'affected_date_values':sorted({day for _,day in cells}),
            'can_execute':not active_conflicts and (not protected or criteria['historical_correction']),
            '_snapshot':snapshot}


@transaction.atomic
def cancel(group,user,data):
    if data.get('confirmed') is not True: raise BusinessError('请确认撤销范围。')
    reason=data.get('reason')
    if not isinstance(reason,str) or not 1<=len(reason.strip())<=500: raise BusinessError('请填写撤销原因，最多500字。')
    lock_group(group)
    report=preview(group,data)
    if data.get('fingerprint')!=report['fingerprint']: raise BusinessError('排班或撤销范围已变化，请重新预览。',409)
    if not report['affected_cells']: raise BusinessError('当前范围没有可撤销的排班记录。')
    if report['active_bed_conflicts']:
        raise BusinessError('所选正式班次正在执行或时间无法核实，且关联已发布床位责任；本系统尚无安全交接确认流程，不能撤销。',422)
    if report['protected_records'] and not report['criteria']['historical_correction']:
        raise BusinessError('所选范围含已开始或过去的正式班次，须使用授权历史更正并填写原因。',422)
    snapshot=report.pop('_snapshot')
    action=ScheduleCancellation.objects.create(group=group,actor=user,criteria=report['criteria'],snapshot=snapshot,reason=reason.strip())
    for row in snapshot['published']:
        entry=row['entry'];ScheduleCancellationEntry.objects.create(cancellation=action,group=group,
            publication_id=row['publication'],nurse_id_at_action=entry['nurse'],date=entry['date'],shift=entry['shift'])
    by_draft={}
    for row in snapshot['drafts']:
        entry=row['entry'];by_draft.setdefault(row['draft'],set()).add(_key(entry))
        ScheduleCancellationEntry.objects.create(cancellation=action,group=group,draft_id=row['draft'],
            nurse_id_at_action=entry['nurse'],date=entry['date'],shift=entry['shift'])
    for draft in ScheduleDraft.objects.filter(group=group,id__in=by_draft):
        draft.entries=[entry for entry in draft.entries if _key(entry) not in by_draft[draft.id]]
        draft.revision+=1;draft.save(update_fields=['entries','revision','updated_at'])
    for row in snapshot['bed_drafts']:
        BedDraft.objects.filter(group=group,id=row['id']).update(revision=F('revision')+1,updated_at=timezone.now())
    cells={(row['entry']['nurse'],row['entry']['date']) for row in snapshot['published']+snapshot['drafts']}
    for nurse_id,day in cells:
        ScheduleCancellationHold.objects.update_or_create(group=group,nurse_id_at_action=nurse_id,date=day,
            defaults={'cancellation':action,'active':True,'resolved_at':None})
    audit(user,group,'撤销指定时段排班',{'cancellation':action.id,'criteria':report['criteria'],
        'affected_cells':len(cells),'published_records':report['published_records'],
        'draft_records':report['draft_records'],'related_requests':report['related_requests'],'reason':reason.strip(),
        'historical_correction':report['criteria']['historical_correction'],'protected_records':report['protected_records'],
        'related_bed_publications':report['related_bed_publications'],'related_bed_drafts':report['related_bed_drafts']})
    return {**report,'id':action.id,'created_at':action.created_at.isoformat(),
            'message':'已撤销所选有效排班；历史发布快照与调班申请保留，相关床位分配须重新同步核对。'}
