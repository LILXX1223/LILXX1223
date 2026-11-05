import hashlib
import json
from collections import Counter
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from ortools.sat.python import cp_model
from scheduling.models import Nurse, Bed, Patient, BedDraft, BedPublication, Skill, NurseSkill, SkillDeletionSnapshot, AuditLog
from scheduling.api.serializers import NurseSerializer, PatientSerializer
from scheduling.services import schedule
from scheduling.services.patients import record_version
from scheduling.services.skills import BUILTINS
from scheduling.services.common import BusinessError, lock_group, require_revision, audit, notify
from scheduling.services.bed_configuration import number_sort_key, public_number

WEIGHTS={'light':1,'medium':2,'high':4}
SPECIAL=['CRRT','ECMO','机械通气','PICC','气道护理','俯卧位护理','血液净化']


def on_duty(group,day,shift):
    pub=schedule.current_publication(group,day)
    # Use only this date and exact published shift. In particular, DAY staff are
    # not silently added to A or N; each roster is separately authorized.
    # Bind roster and publication to the same immutable snapshot. Looking up
    # the latest version twice could mix versions if a publication arrives
    # between the queries.
    entries=schedule.effective_publication_entries(pub,day) if pub else []
    ids=[e['nurse'] for e in entries if e.get('date')==str(day)
         and e.get('shift')==shift and e.get('working',True) is not False]
    nurses=list(Nurse.objects.filter(group=group,id__in=ids,status='active',can_manage_beds=True,effective_from__lte=day))
    nurses=[n for n in nurses if shift in n.qualifications]
    return pub,nurses


def patients(group):
    return sorted(Patient.objects.filter(group=group,bed__isnull=False).select_related('bed'),
                  key=lambda patient: number_sort_key(patient.bed.number))


def cancellation_after_sync(draft,pub):
    if not draft.pk or not pub or draft.schedule_publication_id!=pub.id:return False
    from scheduling.cancellation_models import ScheduleCancellationEntry
    last=ScheduleCancellationEntry.objects.filter(group=draft.group,publication=pub,date=draft.date,shift=draft.shift).select_related('cancellation').order_by('-cancellation__created_at','-id').first()
    if not last:return False
    synced=AuditLog.objects.filter(group=draft.group,action='同步当班护士',details__draft=draft.pk).order_by('-created_at','-id').first()
    return not synced or last.cancellation.created_at>synced.created_at


def fingerprint(draft):
    pub,nurses=on_duty(draft.group,draft.date,draft.shift)
    from scheduling.cancellation_models import ScheduleCancellationEntry
    cancelled=list(ScheduleCancellationEntry.objects.filter(group=draft.group,publication=pub,date=draft.date,shift=draft.shift).order_by('id').values_list('id',flat=True)) if pub else []
    value={'publication':pub.id if pub else None,'nurses':NurseSerializer(nurses,many=True).data,
           'patients':PatientSerializer(patients(draft.group),many=True).data,'rules':draft.group.rules,
           'cancelled_schedule_entries':cancelled,'beds':list(Bed.objects.filter(group=draft.group).order_by('id').values('id','number','enabled','configured','visible'))}
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()


def check_context(draft,data):
    if data.get('fingerprint')!=fingerprint(draft): raise BusinessError('人员、患者或正式排班已改变，请刷新并重新校验。',409)


def qualified(nurse,patient,day,rules):
    treatments=patient.treatments
    catalog={skill.code:skill for skill in Skill.objects.filter(group=nurse.group)}
    # Fixed safety requirements remain requirements even when catalog entries are deleted.
    terms={code:{name} for code,name in BUILTINS}
    for skill in catalog.values():
        if skill.participates_bed or skill.code in terms:
            terms.setdefault(skill.code,set()).update(skill.match_terms or [skill.name])
    for old in SkillDeletionSnapshot.objects.filter(group=nurse.group).order_by('id'):
        if old.participates_bed or old.code in terms:
            terms.setdefault(old.code,set()).update(old.match_terms or [old.name])
    if any('气管插管' in t for t in treatments): terms['AIRWAY'].add('气管插管')
    required={code for code,aliases in terms.items() if any(isinstance(alias,str) and alias.casefold() in treatment.casefold()
              for alias in aliases for treatment in treatments if isinstance(treatment,str))}
    links={x.skill.code:x for x in NurseSkill.objects.filter(nurse=nurse,skill__enabled=True).select_related('skill')}
    legacy={x.get('name'):x.get('expires_on','') for x in nurse.certifications if isinstance(x,dict)}
    for code in required:
        link=links.get(code)
        expiry=legacy.get(link.skill.name,'') if link else ''
        if not link or not link.independent or not link.expires_on or link.expires_on<day or not isinstance(expiry,str) or expiry<str(day):
            return False
    if patient.load=='high' and nurse.years<rules['high_min_years']: return False
    return True


def validate(draft, assignments=None, partial=False):
    assignments=draft.assignments if assignments is None else assignments
    pub,nurses=on_duty(draft.group,draft.date,draft.shift); ns={n.id:n for n in nurses}; ps=patients(draft.group)
    errors=[]; warnings=[]; assigned={}; seen=set(); rules=draft.group.rules
    def err(message,bed=None): errors.append({'message':message,'bed':bed})
    if not pub: err('该日期还没有正式排班，请先审核发布排班。')
    if not draft.schedule_publication_id or (pub and draft.schedule_publication_id!=pub.id): err('请同步最新正式排班中的当班护士。')
    if cancellation_after_sync(draft,pub):err('正式排班已撤销部分班次，请重新同步当班护士。')
    if set(draft.roster)!=set(ns): err('当班护士资质或状态已改变，请重新同步。')
    valid_beds={str(p.bed_id) for p in ps}
    if any(k not in valid_beds for k in assignments): err('分配记录包含空床或不存在的床位。')
    for p in ps:
        if p.id in seen: err('同一患者重复占床',p.bed_id)
        seen.add(p.id)
        if not p.reviewed_by_id: err(str(p.bed.number)+'床患者信息尚未人工核对',p.bed_id)
        if not p.bed.enabled: err('在床患者对应床位已停用',p.bed_id)
        if not p.bed.configured or not p.bed.visible: err(str(p.bed.number)+'床有在床患者但未显示或不在配置范围，请先处理。',p.bed_id)
        nid=assignments.get(str(p.bed_id)); nurse=ns.get(nid)
        if nid is None:
            if not partial: err(str(p.bed.number)+'床尚未分配责任护士',p.bed_id)
            continue
        if not nurse: err(str(p.bed.number)+'床责任护士不在可用当班名单中',p.bed_id); continue
        if not qualified(nurse,p,draft.date,rules): err(nurse.name+' 不具备患者当前治疗所需有效独立资质或年资',p.bed_id)
        assigned.setdefault(nid,[]).append(p)
    for nid,items in assigned.items():
        nurse=ns[nid]; n=len(items); loads=[p.load for p in items]; total=sum(WEIGHTS[p.load] for p in items)
        if n>min(3,rules['max_beds']): err(nurse.name+' 超过最大管床数')
        if total>rules['max_load']: err(nurse.name+' 总护理负荷超过配置上限')
        if n==3:
            legal=(loads.count('light')==3 and rules['allow_three_light']) or (loads.count('light')==2 and loads.count('medium')==1 and rules['allow_two_light_medium'])
            if not legal or nurse.years<rules['triple_min_years']: err(nurse.name+' 三床组合或年资不符合规则')
            else: warnings.append({'message':nurse.name+' 分管3床，须由负责人最终确认。'})
    return {'valid':not errors,'errors':errors,'warnings':warnings,'revision':draft.revision,'fingerprint':fingerprint(draft)}


@transaction.atomic
def sync(draft,user,data):
    lock_group(draft.group); draft.refresh_from_db(); require_revision(draft,data.get('revision')); check_context(draft,data)
    pub,nurses=on_duty(draft.group,draft.date,draft.shift)
    if not pub: raise BusinessError('请先发布该日期的排班。')
    if not nurses: raise BusinessError('当前日期与班次未找到有效的正式当班护士记录，请核查排班发布情况。',422)
    draft.roster=[n.id for n in nurses]; draft.schedule_publication=pub; draft.revision+=1; draft.save()
    audit(user,draft.group,'同步当班护士',{'draft':draft.id,'publication':pub.id,'roster':draft.roster})
    return draft


def recommend(draft, progress=None, before_solve=None, experience_ids=(), preference_snapshot=None):
    """Build a preview; progress reports only real service and solver events.

    Stages are shared with the task manager. Stage 4 (AI analysis) belongs to
    that manager and is intentionally never claimed by this rule-only solver.
    """
    def emit(stage,status,summary):
        if progress: progress(stage,status,summary)
    emit(1,'running',{'date':str(draft.date),'shift':draft.shift})
    pub,nurses=on_duty(draft.group,draft.date,draft.shift)
    ps=patients(draft.group)
    if not pub:
        emit(1,'failed',{'reason':'no_schedule_publication'})
        raise BusinessError('该日期还没有正式排班，请先审核发布排班。',422)
    if not nurses:
        emit(1,'failed',{'reason':'no_on_duty_nurses','schedule_publication':pub.id})
        raise BusinessError('当前日期与班次未找到有效的正式当班护士记录，请核查排班发布情况。',422)
    emit(1,'completed',{'schedule_publication':pub.id,'nurses':len(nurses),'patients':len(ps),
                        'unassigned':sum(str(p.bed_id) not in draft.assignments for p in ps),
                        'revision':draft.revision})
    emit(2,'running',{})
    pre=validate(draft,{},partial=True)
    if not pre['valid']:
        emit(2,'failed',{'reason':'context_or_review_invalid','errors':pre['errors']})
        raise BusinessError('无法生成合规方案，请先完成同步及患者核对。',422,pre['errors'])
    if not ps:
        emit(2,'failed',{'reason':'no_patients'})
        raise BusinessError('当前没有在床患者。')
    emit(2,'completed',{'reviewed':len(ps),'errors':0,'patient_loads':dict(Counter(p.load for p in ps))})
    initial_revision=draft.revision; initial_fingerprint=pre['fingerprint']
    rules=draft.group.rules
    from scheduling.services.experiences import resolve_weights
    defaults={'load_balance':1.0,'minimize_three':0.5,'continuity':0.1,'assessment_match':0.0}
    preference=preference_snapshot if preference_snapshot is not None else resolve_weights(draft.group,'BED_ASSIGNMENT',defaults,experience_ids)
    emit(3,'running',{})
    eligibility={(i,j):qualified(n,p,draft.date,rules) for i,p in enumerate(ps) for j,n in enumerate(nurses)}
    unmatched=[public_number(p.bed.number) for i,p in enumerate(ps) if not any(eligibility[i,j] for j in range(len(nurses)))]
    if unmatched:
        emit(3,'failed',{'reason':'missing_qualified_nurse','beds':unmatched})
        raise BusinessError('无法生成合规方案：以下床位没有满足当前治疗资格或年资要求的当班护士：'+
                            '、'.join(str(n)+'床' for n in unmatched),422)
    # Only compare ordinal ranks when every eligible candidate for a reviewed
    # higher-need patient has a recorded rank. An unknown rank is not an
    # invented low rank, nor should it accidentally be rewarded as rank 1.
    comparable=[i for i,p in enumerate(ps) if p.reviewed_by_id and p.assessment_need=='higher'
        and all(n.assessment_level in range(1,6) for j,n in enumerate(nurses) if eligibility[i,j])]
    higher=sum(p.reviewed_by_id is not None and p.assessment_need=='higher' for p in ps)
    if not comparable:
        preference={**preference,'weights':dict(preference['weights']),'applied':list(preference['applied']), 'excluded':list(preference['excluded'])}
        for entry in list(preference['applied']):
            if entry['target']=='assessment_match':
                preference['applied'].remove(entry)
                preference['excluded'].append({'id':entry['id'],'reason':'无可比较的已核对较高评估需求与完整护士能级'})
        preference['weights']['assessment_match']=0.0
    emit(3,'completed',{'nurses':len(nurses),'patients':len(ps),'eligible_pairs':sum(eligibility.values()),
                        'excluded_pairs':len(eligibility)-sum(eligibility.values()),
                        'weights':preference['weights'],'applied_experiences':preference['applied'],'excluded_experiences':preference['excluded'],
                        'assessment_higher_need':higher,'assessment_comparable':len(comparable),
                        'assessment_pending':sum(not p.assessment_need for p in ps),'nurse_assessment_missing':sum(n.assessment_level is None for n in nurses)})
    # The optional caller-owned AI step is deliberately outside a write
    # transaction, and cannot alter or bypass these qualification constraints.
    if before_solve:
        before_solve(draft,nurses,ps)
        draft.refresh_from_db(); draft.group.refresh_from_db()
        if draft.revision!=initial_revision or fingerprint(draft)!=initial_fingerprint:
            raise BusinessError('AI分析期间人员、患者或排班已改变，请刷新后重新生成方案。',409)
    model=cp_model.CpModel(); x={}
    for i,p in enumerate(ps):
        for j,n in enumerate(nurses):
            x[i,j]=model.NewBoolVar(f'p{i}n{j}')
            if not eligibility[i,j]: model.Add(x[i,j]==0)
        model.Add(sum(x[i,j] for j in range(len(nurses)))==1)
    loads=[]; over=[]; retained=[]
    for j,n in enumerate(nurses):
        count=sum(x[i,j] for i in range(len(ps))); model.Add(count<=min(3,rules['max_beds']))
        triple=model.NewBoolVar(f'triple{j}'); model.Add(count==3).OnlyEnforceIf(triple); model.Add(count<=2).OnlyEnforceIf(triple.Not())
        if n.years<rules['triple_min_years']: model.Add(triple==0)
        highs=sum(x[i,j] for i,p in enumerate(ps) if p.load=='high')
        mids=sum(x[i,j] for i,p in enumerate(ps) if p.load=='medium')
        model.Add(highs==0).OnlyEnforceIf(triple); model.Add(mids<=1).OnlyEnforceIf(triple)
        if not rules['allow_three_light']: model.Add(mids==1).OnlyEnforceIf(triple)
        if not rules['allow_two_light_medium']: model.Add(mids==0).OnlyEnforceIf(triple)
        load=model.NewIntVar(0,rules['max_load'],f'load{j}'); model.Add(load==sum(WEIGHTS[p.load]*x[i,j] for i,p in enumerate(ps))); loads.append(load)
        over.append(triple)
        for i,p in enumerate(ps):
            if draft.assignments.get(str(p.bed_id))==n.id: retained.append(x[i,j])
    maximum=model.NewIntVar(0,rules['max_load'],'maximum'); model.AddMaxEquality(maximum,loads)
    # Normalize each soft objective to 0..10000, then apply 0..1 weights.
    # Capacity, treatment qualification and three-bed safety remain hard constraints.
    objective=[]
    def term(name,expression,bound):
        if not bound:return
        raw=model.NewIntVar(0,bound,'raw_'+name)
        model.Add(raw==expression)
        scaled=model.NewIntVar(0,10000,'normalized_'+name)
        model.AddDivisionEquality(scaled,raw*10000,bound)
        objective.append(int(round(preference['weights'][name]*10))*scaled)
    term('load_balance',maximum,rules['max_load'])
    term('minimize_three',sum(over),len(nurses))
    term('continuity',len(retained)-sum(retained),len(retained))
    term('assessment_match',sum((n.assessment_level-1)*x[i,j] for i in comparable for j,n in enumerate(nurses)
        if eligibility[i,j] and n.assessment_level in range(1,6)),4*len(comparable))
    model.Minimize(sum(objective))
    solver=cp_model.CpSolver(); solver.parameters.max_time_in_seconds=5; solver.parameters.num_search_workers=1
    started_at=timezone.now().isoformat()
    emit(5,'running',{'started_at':started_at,'patients':len(ps),'nurses':len(nurses),'max_seconds':5})
    try: status=solver.Solve(model)
    except Exception:
        emit(5,'failed',{'reason':'solver_exception'})
        raise BusinessError('床位分配求解器执行失败，请重试或联系管理员。',503)
    elapsed=solver.WallTime()
    outcome={cp_model.OPTIMAL:'optimal',cp_model.FEASIBLE:'feasible',cp_model.INFEASIBLE:'infeasible',
             cp_model.MODEL_INVALID:'model_invalid',cp_model.UNKNOWN:'unknown'}.get(status,'unknown')
    if elapsed>=4.99 and status==cp_model.FEASIBLE: outcome='timeout_feasible'
    if elapsed>=4.99 and status==cp_model.UNKNOWN: outcome='timeout_no_solution'
    solver_details={'status':outcome,'raw_status':solver.StatusName(status),'started_at':started_at,
                    'elapsed_seconds':round(elapsed,4),'max_seconds':5,'weights':preference['weights'],
                    'coefficients':{k:int(round(v*10)) for k,v in preference['weights'].items()},
                    'applied_experience_ids':[e['id'] for e in preference['applied']]}
    if status not in [cp_model.OPTIMAL,cp_model.FEASIBLE]:
        emit(5,'failed',solver_details)
        if status==cp_model.INFEASIBLE:
            raise BusinessError('无法生成合规方案：人数、资质或负荷约束不能同时满足，请调配人员。',422)
        if status==cp_model.MODEL_INVALID: raise BusinessError('床位分配模型无效，请联系管理员。',503)
        raise BusinessError('床位分配求解未找到可行方案，请调整条件后重试。',422)
    emit(5,'completed',solver_details)
    assignment={str(p.bed_id):n.id for i,p in enumerate(ps) for j,n in enumerate(nurses) if solver.Value(x[i,j])}
    # Solving runs without a write lock. Do not label a proposal derived from old
    # patients or qualifications with a new dependency fingerprint.
    draft.refresh_from_db(); draft.group.refresh_from_db()
    if draft.revision!=initial_revision or fingerprint(draft)!=initial_fingerprint:
        raise BusinessError('求解期间人员、患者或排班已改变，请刷新后重新生成方案。',409)
    emit(6,'running',{})
    validation=validate(draft,assignment)
    if not validation['valid']:
        emit(6,'failed',{'errors':validation['errors']})
        raise BusinessError('候选分配未通过最终校验。',422,validation['errors'])
    nurse_metrics=[]
    for nurse in nurses:
        assigned_patients=[p for p in ps if assignment.get(str(p.bed_id))==nurse.id]
        nurse_metrics.append({'nurse_id':nurse.id,'patients':len(assigned_patients),
            'load':sum(WEIGHTS[p.load] for p in assigned_patients),
            'high_load_patients':sum(p.load=='high' for p in assigned_patients)})
    assessment_matches=[{'bed_id':ps[i].bed_id,'nurse_id':assignment[str(ps[i].bed_id)],
        'assessment_level':next(n.assessment_level for n in nurses if n.id==assignment[str(ps[i].bed_id)])} for i in comparable]
    evaluation={'patient_count':len(ps),'assigned':len(assignment),'unassigned':len(ps)-len(assignment),
                'assessment_match':{'higher_need_count':higher,'comparable_count':len(comparable),
                    'unconfirmed_need_count':sum(not p.assessment_need for p in ps),
                    'matches':assessment_matches,
                    'mean_ordinal_level':round(sum(row['assessment_level'] for row in assessment_matches)/len(assessment_matches),2) if assessment_matches else None},
                'nurses':nurse_metrics,'hard_constraints_valid':validation['valid']}
    emit(6,'completed',{'valid':True,'warnings':validation['warnings'],'evaluation':evaluation})
    emit(7,'completed',{'assigned_patients':len(assignment),'nurses':len(set(assignment.values())),
                        'revision':initial_revision})
    return {'assignments':assignment,'fingerprint':initial_fingerprint,'revision':draft.revision,
            'effective_weights':preference['weights'],'applied_experiences':preference['applied'],'excluded_experiences':preference['excluded'],
            'reason':'OR-Tools 按岗位、有效独立资质、年资、三床组合和护理负荷约束求解，并兼顾负荷均衡与照护连续性。',
            'solver':solver_details,'validation':validation,'evaluation':evaluation}


@transaction.atomic
def modify(draft,user,data,record_adjustment=False):
    lock_group(draft.group); draft.refresh_from_db(); require_revision(draft,data.get('revision')); check_context(draft,data)
    values=data.get('assignments')
    if not isinstance(values,dict) or any(not isinstance(k,str) or type(v) is not int for k,v in values.items()): raise BusinessError('责任护士分配格式无效。')
    if not str(data.get('reason','')).strip(): raise BusinessError('请填写调整原因。')
    result=validate(draft,values,partial=True)
    if not result['valid']: raise BusinessError('调整违反安全约束，未保存。',422,result['errors'])
    before=draft.assignments; draft.assignments=values; draft.revision+=1; draft.save()
    audit(user,draft.group,'修改床位责任',{'before':before,'after':values,'reason':data['reason'],'revision':draft.revision,'draft':draft.id})
    if record_adjustment and before!=values:
        from scheduling.experience_models import AllocationFeedback
        changed={key for key in set(before)|set(values) if before.get(key)!=values.get(key)}
        # Preserve stable patient IDs, revisions and confirmed categories only;
        # avoid copying names or free-text medical narratives into feedback.
        patient_context=[{'bed_id':p.bed_id,'patient_id':p.id,'patient_revision':p.revision,'load':p.load,
            'reviewed':bool(p.reviewed_by_id),'treatment_categories':[name for name in SPECIAL
                if any(name.casefold() in text.casefold() for text in p.treatments if isinstance(text,str))]}
            for p in patients(draft.group) if str(p.bed_id) in changed]
        context={'date':str(draft.date),'shift':draft.shift,'schedule_publication':draft.schedule_publication_id,
                 'draft_id':draft.id,'draft_revision':draft.revision,'patient_tasks':patient_context}
        AllocationFeedback.objects.create(group=draft.group,created_by=user,business_type='BED_ASSIGNMENT',before=before,after=values,
            reason=data['reason'],context=context,task_reference=f'bed-draft:{draft.id}:{draft.revision}',adopted=False)
    return draft


@transaction.atomic
def publish(draft,user,data):
    lock_group(draft.group); draft.refresh_from_db(); require_revision(draft,data.get('revision'))
    check_context(draft,data)
    existing=BedPublication.objects.filter(draft=draft,draft_revision=draft.revision).first()
    if existing: return existing
    report=validate(draft)
    if not report['valid']: raise BusinessError('存在阻断性异常，不能发布。',422,report['errors'])
    if report['warnings'] and data.get('confirm_three') is not True: raise BusinessError('请确认三床组合的护理安全条件。')
    ns={n.id:n for n in Nurse.objects.filter(group=draft.group)}
    active_patients=list(patients(draft.group))
    for patient in active_patients: record_version(patient,user,'分床发布引用状态')
    rows=[{'patient':p.id,'patient_revision':p.revision,'patient_code':p.code,'bed':p.bed_id,'bed_number':public_number(p.bed.number),'nurse':draft.assignments[str(p.bed_id)],
           'nurse_name':ns[draft.assignments[str(p.bed_id)]].name,'load':p.load,
           'nurse_skill_snapshot':NurseSerializer(ns[draft.assignments[str(p.bed_id)]]).data['skill_details']} for p in active_patients]
    pub=BedPublication.objects.create(group=draft.group,draft=draft,draft_revision=draft.revision,date=draft.date,shift=draft.shift,
        snapshot={'assignments':rows,'rules':draft.group.rules,'validation':report,'schedule_publication':draft.schedule_publication_id},published_by=user)
    from scheduling.experience_models import AllocationFeedback
    from scheduling.services.experiences import BED
    feedback=AllocationFeedback.objects.filter(group=draft.group,business_type=BED,
        task_reference=f'bed-draft:{draft.id}:{draft.revision}',adopted=False).order_by('-id').first()
    if feedback and feedback.after==draft.assignments:
        AllocationFeedback.objects.create(group=draft.group,created_by=user,business_type=BED,
            before=feedback.before,after=feedback.after,context=feedback.context,reason=feedback.reason,task_reference=f'bed-publication:{pub.id}',adopted=True)
    audit(user,draft.group,'发布床位分配',{'publication':pub.id,'revision':draft.revision,'assignments':rows})
    notify(draft.group,str(draft.date)+' '+draft.shift+'班床位分配已发布，版本 #'+str(pub.id))
    return pub
