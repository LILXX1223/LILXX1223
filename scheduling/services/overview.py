"""Current, group-scoped overview derived from existing business records."""
import logging
from collections import Counter
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from scheduling.cancellation_models import ScheduleCancellation, ScheduleCancellationEntry
from scheduling.models import (Bed, BedDraft, BedPublication, Nurse, NurseSkill, Patient,
                               ScheduleDraft, ShiftRequest)
from scheduling.services import beds, schedule


logger = logging.getLogger(__name__)
WORKING_SHIFTS = ('A', 'DAY', 'N')


def _weekly_entries(group, week):
    """Each date uses the same version-selection and cancellation rules as beds."""
    publications = {day: schedule.current_publication(group, day)
                    for day in schedule.dates(week)}
    unique = {pub.id: pub for pub in publications.values() if pub}
    by_version = {pk: schedule.effective_publication_entries(pub) for pk, pub in unique.items()}
    rows = [entry for day, pub in publications.items() if pub
            for entry in by_version[pub.id] if entry.get('date') == str(day)]
    original = sum(1 for day,pub in publications.items() if pub
                   for entry in pub.snapshot.get('entries',[]) if entry.get('date') == str(day))
    return publications, unique, rows, original


def _validate_formal(group, week, rows, revision):
    if not rows:
        return {'status':'no_effective_schedule','blocking':None,'warnings':None,
                'message':'当前周期暂无有效正式排班'}
    # The same validator used before publication, applied to the effective
    # immutable snapshots and current rules without saving a draft.
    try:
        draft = ScheduleDraft(group=group, start_date=week, revision=revision, entries=rows)
        report = schedule.validate(draft)
        blocking, warnings = len(report['errors']), len(report['warnings'])
        return {'status':'blocking' if blocking else 'warnings' if warnings else 'passed',
                'blocking':blocking, 'warnings':warnings, 'message':None}
    except Exception:
        # A malformed legacy snapshot must not erase the other overview panels.
        # No clinical records, names or original exception text are returned.
        logger.exception('overview_schedule_validation_failed group=%s',group.pk)
        return {'status':'unavailable','blocking':None,'warnings':None,
                'message':'正式排班校验暂不可用，请在排班管理中核查。'}


def _current_shifts(group, now):
    """Use actual configured shift times, including the previous day's N shift."""
    found=[]
    for day in (now.date()-timedelta(days=1), now.date()):
        for code,shift in schedule.effective_shifts(group,day).items():
            if code not in WORKING_SHIFTS or not shift.enabled or not shift.working:
                continue
            start=datetime.combine(day,shift.start,tzinfo=now.tzinfo)
            end=datetime.combine(day+timedelta(days=int(shift.next_day)),shift.end,tzinfo=now.tzinfo)
            if end>start and start<=now<end:
                found.append((day,code,shift.name))
    return found


def _bed_alerts(group, now, patient_rows):
    alerts=[]
    if not patient_rows: return alerts
    unreviewed=sum(not p.reviewed_by_id for p in patient_rows)
    if unreviewed:
        alerts.append({'level':'warning','title':'患者护理资料待核对',
                       'detail':f'{unreviewed} 名在床患者的资料尚未人工核对。','route':'/beds'})
    invalid_beds=sum(not p.bed.enabled or not p.bed.configured or not p.bed.visible for p in patient_rows)
    if invalid_beds:
        alerts.append({'level':'danger','title':'在床患者所在床位状态异常',
                       'detail':f'{invalid_beds} 张床位未开放、未配置或未设置显示，需先核查。','route':'/beds'})
    for day,code,name in _current_shifts(group,now):
        publication,nurses=beds.on_duty(group,day,code)
        entries=schedule.effective_publication_entries(publication,day) if publication else []
        scheduled=any(e.get('shift')==code and e.get('working',True) is not False for e in entries)
        minimum=group.rules.get('min_staff',{}).get(code)
        if not scheduled and not (type(minimum) is int and minimum>0):
            continue
        if not nurses:
            alerts.append({'level':'warning','title':'当前班次无有效分床护士',
                           'detail':f'{day} {name}未找到有效正式当班护士，请核对排班和资格。',
                           'route':'/beds','date':str(day),'shift':code})
            continue
        qualification_cache={}
        def qualifies(nurse, patient):
            # Equal current treatment lists and load produce the same result for
            # a nurse; reuse it within this shift to avoid repeated ORM lookups.
            key=(nurse.id,patient.load,tuple(patient.treatments))
            if key not in qualification_cache:
                qualification_cache[key]=beds.qualified(nurse,patient,day,group.rules)
            return qualification_cache[key]
        unmatched=sum(1 for patient in patient_rows if patient.reviewed_by_id and
                      not any(qualifies(nurse,patient) for nurse in nurses))
        if unmatched:
            alerts.append({'level':'danger','title':'当前护理要求缺少合格当班护士',
                           'detail':f'{day} {name}有 {unmatched} 名已核对患者未找到满足现行护理资格或年资规则的护士。',
                           'route':'/beds','date':str(day),'shift':code})
        latest=BedPublication.objects.filter(group=group,date=day,shift=code).order_by('-id').first()
        if not latest:
            alerts.append({'level':'warning','title':'当前班次床位分配未发布',
                           'detail':f'{day} {name}尚无正式分床版本；{len(patient_rows)} 名在床患者尚无本班次正式责任记录。',
                           'route':'/beds','date':str(day),'shift':code})
        else:
            rows=latest.snapshot.get('assignments',[])
            source=latest.snapshot.get('schedule_publication')
            cancelled=ScheduleCancellationEntry.objects.filter(group=group,publication=publication,
                date=day,shift=code,cancellation__created_at__gt=latest.created_at).exists() if publication else False
            eligible={n.id:n for n in nurses}
            if not publication or source!=publication.id or cancelled or any(row.get('nurse') not in eligible for row in rows):
                alerts.append({'level':'warning','title':'床位分配的正式排班来源已变化',
                               'detail':f'{day} {name}的分床版本 #{latest.id} 需核查当班护士和责任安排。',
                               'route':'/beds','date':str(day),'shift':code})
            # Match patient AND stable bed ID; an earlier patient from the same
            # bed or a previous assignment must not hide a missing responsibility.
            assignments={(row.get('patient'),row.get('bed')):row for row in rows if row.get('nurse') is not None}
            missing=[p for p in patient_rows if (p.id,p.bed_id) not in assignments]
            if missing:
                alerts.append({'level':'warning','title':'在床患者缺少当前正式责任护士',
                               'detail':f'{day} {name}有 {len(missing)} 名在床患者未见对应正式分床责任。',
                               'route':'/beds','date':str(day),'shift':code})
            changed=sum(1 for patient in patient_rows if (row:=assignments.get((patient.id,patient.bed_id)))
                        and row.get('patient_revision')!=patient.revision)
            if changed:
                alerts.append({'level':'warning','title':'已发布分床引用的患者资料已变更',
                               'detail':f'{day} {name}有 {changed} 名在床患者的资料版本与发布时不同，需重新核查。',
                               'route':'/beds','date':str(day),'shift':code})
            mismatched=0
            for patient in patient_rows:
                row=assignments.get((patient.id,patient.bed_id))
                nurse=eligible.get(row.get('nurse')) if row else None
                if nurse and patient.reviewed_by_id and not qualifies(nurse,patient): mismatched+=1
            if mismatched:
                alerts.append({'level':'danger','title':'特殊治疗资格或护理能力需重新核查',
                               'detail':f'{day} {name}有 {mismatched} 项当前责任不满足现行资格或年资规则。',
                               'route':'/beds','date':str(day),'shift':code})
        draft=BedDraft.objects.filter(group=group,date=day,shift=code).first()
        if draft and draft.assignments:
            roster={n.id for n in nurses}
            if (not publication or draft.schedule_publication_id!=publication.id or
                set(draft.roster)!=roster or beds.cancellation_after_sync(draft,publication)):
                alerts.append({'level':'warning','title':'床位分配草稿需重新同步排班',
                               'detail':f'{day} {name}的当班护士来源已变化，请重新同步并校验。',
                               'route':'/beds','date':str(day),'shift':code})
            elif not latest or draft.revision!=latest.draft_revision:
                alerts.append({'level':'info','title':'床位分配草稿待校验发布',
                               'detail':f'{day} {name}存在尚未发布的责任安排。',
                               'route':'/beds','date':str(day),'shift':code})
    return alerts


@transaction.atomic
def summary(group):
    now=timezone.localtime(); day=now.date(); week=day-timedelta(days=day.weekday())
    schedule_available=True
    try:
        per_day,versions,entries,original_count=_weekly_entries(group,week)
    except Exception:
        logger.exception('overview_schedule_source_failed group=%s',group.pk)
        per_day,versions,entries,original_count={}, {}, [], 0
        schedule_available=False
    pub=per_day.get(day) or (max(versions.values(),key=lambda item:item.id) if versions else None)
    formal_state=({'status':'cancelled' if not entries else 'partially_cancelled' if len(entries)<original_count else 'published',
                   'original_records':original_count,'effective_records':len(entries),
                   'cancelled_records':original_count-len(entries),
                   'effective_dates':sorted({e['date'] for e in entries})} if pub else None)
    uncovered_dates=[str(value) for value in schedule.dates(week) if str(value) not in {entry['date'] for entry in entries}]
    validation=(_validate_formal(group,week,entries,pub.draft_revision if pub else 0) if schedule_available else
                {'status':'unavailable','blocking':None,'warnings':None,'message':'正式排班数据暂无法读取。'})
    active={n.id:n for n in Nurse.objects.filter(group=group,status='active',effective_from__lte=day)}
    day_rows=[entry for entry in entries if entry.get('date')==str(day)]
    types=schedule.effective_shifts(group,day)
    confirmed=group.rules.get('clinical_thresholds_confirmed') is True
    shifts=[]
    for code in ('A','DAY','N','POST'):
        shift=types.get(code)
        enabled=bool(shift and shift.enabled and (shift.working or code=='POST'))
        ids={entry.get('nurse') for entry in day_rows if entry.get('shift')==code
             and entry.get('nurse') in active and (code=='POST' or code in active[entry['nurse']].qualifications)}
        count=len(ids) if day_rows and enabled else None
        need=group.rules.get('min_staff',{}).get(code) if code!='POST' else None
        if type(need) is not int or need<=0: need=None
        if code=='POST': status='rest'
        elif not schedule_available: status='unavailable'
        elif not enabled: status='disabled'
        elif count is None: status='no_schedule'
        elif need is None: status='unconfigured'
        elif count<need: status='insufficient'
        else: status='meets'
        shifts.append({'code':code,'name':shift.name if shift else code,'enabled':enabled,
            'start':shift.start.strftime('%H:%M') if shift and shift.working else None,
            'end':shift.end.strftime('%H:%M') if shift and shift.working else None,
            'next_day':shift.next_day if shift else False,'count':count,'need':need,'status':status,
            'coverage':round(count/need*100,1) if need and count is not None else None})
    work=[row for row in shifts if row['code'] in WORKING_SHIFTS]
    missing=[row['name'] for row in work if row['status']=='unconfigured']
    if not schedule_available: staffing_status='unavailable'
    elif any(row['status']=='insufficient' for row in work): staffing_status='insufficient'
    elif any(row['status']=='no_schedule' for row in work): staffing_status='no_schedule'
    elif any(row['status'] in ('unconfigured','disabled') for row in work): staffing_status='unconfigured'
    elif validation['status'] in ('blocking','unavailable','no_effective_schedule'): staffing_status='needs_review'
    else: staffing_status='meets'
    staffing={'status':staffing_status,'missing_requirements':missing,'requirements_confirmed':confirmed,
              'note':'仅反映已配置的人数规则，不代表实际到岗，也不替代临床护理安全判断。'}

    nurses=list(Nurse.objects.filter(group=group,effective_from__lte=day).exclude(status='left'))
    levels=Counter(n.level or '其他' for n in nurses); ids=[n.id for n in nurses]; skills={}
    for link in NurseSkill.objects.filter(nurse_id__in=ids,skill__group=group,skill__enabled=True).select_related('skill','nurse'):
        row=skills.setdefault(link.skill_id,{'name':link.skill.name,'skilled':0,'independent':0})
        row['skilled']+=1
        if link.nurse.status=='active' and link.independent and link.expires_on and link.expires_on>=day and any(
                isinstance(cert,dict) and cert.get('name')==link.skill.name and str(cert.get('expires_on',''))>=str(day)
                for cert in (link.nurse.certifications if isinstance(link.nurse.certifications,list) else [])):row['independent']+=1

    patient_rows=beds.patients(group); patients=len(patient_rows)
    pending=ShiftRequest.objects.filter(group=group,is_deleted=False,decision='pending',invalidated_by_deletion=False).count()
    draft=ScheduleDraft.objects.filter(group=group,start_date=week).first()
    has_pending_draft=bool(schedule_available and draft and draft.entries and (not pub or draft.id!=pub.draft_id or draft.revision!=pub.draft_revision))
    alerts=[]
    if pending:alerts.append({'level':'warning','title':'待审核调班申请','detail':f'{pending} 项申请等待审核','route':'/scheduling'})
    if not schedule_available:
        alerts.append({'level':'warning','title':'正式排班数据暂不可用','detail':'暂时无法核对本周正式排班状态，请刷新或联系管理员。','route':'/scheduling'})
    elif not entries:
        alerts.append({'level':'warning','title':'本周暂无有效正式排班','detail':'请核查并发布排班','route':'/scheduling'})
    elif formal_state and formal_state['status']=='partially_cancelled':
        alerts.append({'level':'warning','title':'正式排班部分撤销，待重新校验',
            'detail':f'本周剩余 {len(entries)} 条有效班次，请核对后重新发布。','route':'/scheduling'})
    if entries and uncovered_dates:
        alerts.append({'level':'warning','title':'本周部分日期暂无有效正式班次',
                       'detail':'未见有效班次的日期：'+'、'.join(uncovered_dates),'route':'/scheduling'})
    if has_pending_draft:
        alerts.append({'level':'info','title':'存在待审核排班草稿','detail':'当前草稿尚未成为正式排班。','route':'/scheduling'})
    unavailable_roster={e.get('nurse') for e in day_rows if e.get('shift') in WORKING_SHIFTS
                        and e.get('nurse') is not None and e.get('nurse') not in active}
    if unavailable_roster:
        alerts.append({'level':'warning','title':'正式排班含当前不可用人员',
            'detail':f'今日有 {len(unavailable_roster)} 名排班护士无有效在岗档案，不计入有效排班人数。','route':'/scheduling'})
    if validation['status']=='blocking':
        alerts.append({'level':'danger','title':'正式排班存在阻断问题',
            'detail':f"按当前规则核查发现 {validation['blocking']} 项阻断问题，请进入排班管理处理。",'route':'/scheduling'})
    elif validation['status']=='unavailable' and schedule_available:
        alerts.append({'level':'warning','title':'正式排班校验暂不可用','detail':validation['message'],'route':'/scheduling'})
    elif validation['status']=='warnings':
        alerts.append({'level':'info','title':'正式排班有待核对提示',
                       'detail':f"当前规则核查有 {validation['warnings']} 项提示，请在排班管理中核对。",'route':'/scheduling'})
    for item in shifts:
        if item['status']=='insufficient':
            alerts.append({'level':'warning','title':item['name']+'低于当前配置人数',
                'detail':f"有效排班 {item['count']} 人，配置最低 {item['need']} 人"+('（规则尚未由科室确认）。' if not confirmed else '。'),
                'route':'/scheduling'})
    hidden_open=Bed.objects.filter(group=group,enabled=True).exclude(configured=True,visible=True).count()
    if hidden_open:
        alerts.append({'level':'info','title':'部分开放床位未设置显示',
            'detail':f'{hidden_open} 张开放床位未在工作台配置为显示，仍计入开放床位。','route':'/settings'})
    bed_status_available=True
    try:
        alerts.extend(_bed_alerts(group,now,patient_rows))
    except Exception:
        bed_status_available=False
        logger.exception('overview_bed_status_failed group=%s',group.pk)
        alerts.append({'level':'warning','title':'床位分配状态暂不可用',
                       'detail':'暂时无法核对当前责任安排，请进入床位分配查看。','route':'/beds'})
    alerts.sort(key=lambda item:{'danger':0,'warning':1,'info':2}.get(item['level'],3))

    formal=None
    if pub:
        last_cancel=ScheduleCancellation.objects.filter(group=group,entries__publication_id__in=versions).select_related('actor').order_by('-id').first()
        formal={'id':pub.id,'group_name':group.name,'start_date':str(pub.start_date),'created_at':pub.created_at.isoformat(),
            'end_date':str(pub.start_date+timedelta(days=pub.duration_days-1)),'duration_days':pub.duration_days,
            'published_by':pub.published_by.get_username(),**formal_state,
            'version_ids':sorted(versions),'multiple_versions':len(versions)>1,
            'uncovered_dates':uncovered_dates,
            'last_cancellation':{'id':last_cancel.id,'at':last_cancel.created_at.isoformat(),
                'actor':last_cancel.actor.get_username(),'reason':last_cancel.reason} if last_cancel else None,
            'nurses':list({e['nurse']:{'id':e['nurse'],'name':e.get('name','历史护士')} for e in entries if e.get('nurse') is not None}.values()),
            'nurse_count':len({e.get('nurse') for e in entries if e.get('nurse') is not None}),
            'subgroups':len({e.get('subgroup') for e in entries if e.get('base_type')=='rotation' and e.get('subgroup') is not None}),
            'days':7,'anomalies':(validation['blocking']+validation['warnings'] if validation['blocking'] is not None else None),
            'validation':validation}
    return {'group':{'id':group.id,'name':group.name},'date':str(day),'updated_at':now.isoformat(),
        'week_start':str(week),'has_pending_draft':has_pending_draft,
        'beds':Bed.objects.filter(group=group,enabled=True).count(),'patients':patients,
        'synthetic_patients':sum(p.synthetic for p in patient_rows),'non_synthetic_patients':sum(not p.synthetic for p in patient_rows),
        # admitted_at is record creation and discharged_at records leaving a bed;
        # neither is a verified hospital admission/transfer/discharge event.
        'admissions':None,'discharges':None,'publication':formal,'validation':validation,
        'shifts':shifts,'staffing':staffing,'requirements_confirmed':confirmed,
        'unavailable_roster_count':len(unavailable_roster),'nurses_total':len(nurses),'levels':dict(levels),
        'skills':sorted(skills.values(),key=lambda x:(-x['skilled'],x['name'])),
        'pending_requests':pending,'alerts':alerts,
        'data_quality':{'admission_events':'unavailable','discharge_events':'unavailable',
                        'attendance':'unavailable','schedule':'available' if schedule_available else 'unavailable',
                        'bed_assignment':'available' if bed_status_available else 'unavailable',
                        'schedule_validation':validation['status']}}
