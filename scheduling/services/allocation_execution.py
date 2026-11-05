"""Allocation execution with persisted, factual progress and no clinical-text logs."""
import json
import os
from collections import Counter
from datetime import timedelta
from threading import Thread

from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, transaction
from django.db.models import F
from django.utils import timezone

from scheduling.models import BedDraft, CareGroup
from scheduling.task_models import AllocationEvent, AllocationExecution
from scheduling.services.common import BusinessError, require_revision, scope, lock_group, audit


def event(task, stage, status, summary=None):
    with transaction.atomic():
        changed=AllocationExecution.objects.filter(pk=task.pk,status__in=['queued','running']).update(current_stage=stage)
        if not changed: raise BusinessError('任务已失效，请重新生成。',409)
        AllocationEvent.objects.create(task=task, stage=stage, status=status, summary=summary or {})


def public_task(task, after=0):
    return {'id':task.id,'group':task.group_id,'kind':task.kind,'status':task.status,
        'stage':task.current_stage,'created_at':task.created_at.isoformat(),
        'started_at':task.started_at.isoformat() if task.started_at else None,
        'completed_at':task.completed_at.isoformat() if task.completed_at else None,
        'error':task.error,'result':task.result,'parameters':task.parameters,
        'events':[{'id':e.id,'stage':e.stage,'status':e.status,'summary':e.summary,
                   'at':e.created_at.isoformat()} for e in task.events.filter(id__gt=after)]}


def compare_tasks(left,right,left_plan='A',right_plan='A'):
    """Compare stored facts only, never objective scores with different weights."""
    if left.group_id!=right.group_id or left.kind!=right.kind or left.status!='completed' or right.status!='completed':
        raise BusinessError('任务不属于相同业务范围或尚未完成。',409)
    if left.kind=='GROUP_ASSIGNMENT':
        from scheduling.models import AIGroupingTask
        a=AIGroupingTask.objects.filter(group=left.group,pk=left.result.get('grouping_task_id')).first()
        b=AIGroupingTask.objects.filter(group=left.group,pk=right.result.get('grouping_task_id')).first()
        if not a or not b:raise BusinessError('历史候选分组不存在。',404)
        if a.fingerprint!=b.fingerprint or a.parameters.get('fingerprint_version')!=b.parameters.get('fingerprint_version'):
            raise BusinessError('两轮人员、规则或指纹口径不同，不能按同一数据集比较。',409)
        # Different group sizes, locks or constraints change the feasible set.
        hard=['group_count','group_sizes','min_leaders','mentor_ids','locked','same_pairs','avoid_pairs','junior_years']
        if any(a.parameters.get(k)!=b.parameters.get(k) for k in hard):
            raise BusinessError('两轮分组约束不同，无法进行同条件对比。',409)
        pa=next((p for p in a.plans if p['code']==left_plan),None)
        pb=next((p for p in b.plans if p['code']==right_plan),None)
        if not pa or not pb or not pa.get('applicable',True) or not pb.get('applicable',True):raise BusinessError('所选候选方案不可用。')
        before,after=pa['assignments'],pb['assignments']
        rows=[{'task_id':left.id,'plan':left_plan,'metrics':pa['metrics'],'weights':pa.get('effective_weights',{}),
               'solver':pa['solver_status'],'constraints_valid':pa['validation']['valid']},
              {'task_id':right.id,'plan':right_plan,'metrics':pb['metrics'],'weights':pb.get('effective_weights',{}),
               'solver':pb['solver_status'],'constraints_valid':pb['validation']['valid']}]
    else:
        if left.result.get('fingerprint')!=right.result.get('fingerprint') or left.result.get('revision')!=right.result.get('revision') or not left.result.get('fingerprint'):
            raise BusinessError('两轮床位、患者、当班护士或草稿状态不同，不能按同一数据集比较。',409)
        before,after=left.result['assignments'],right.result['assignments']
        rows=[{'task_id':t.id,'metrics':t.result.get('evaluation',{}),'weights':t.result.get('effective_weights',{}),
               'solver':t.result.get('solver',{}).get('status'),'constraints_valid':t.result.get('validation',{}).get('valid')} for t in (left,right)]
    changed=[{'id':key,'before':before.get(key),'after':after.get(key)} for key in sorted(set(before)|set(after)) if before.get(key)!=after.get(key)]
    return {'kind':left.kind,'rounds':rows,'changed':changed,
            'note':'指标使用同一数据口径；权重不同的求解目标总分不可直接比较。方案变化不证明由某一条经验造成。'}


def start(group, user, kind, data, dispatch=True):
    if kind not in ('GROUP_ASSIGNMENT','BED_ASSIGNMENT'):
        raise BusinessError('任务类型无效。')
    expire_stale(group,kind)
    if data.get('consent') is not True or type(data.get('synthetic')) is not bool:
        raise BusinessError('请明确资料来源，并确认符合外发要求。')
    if kind=='GROUP_ASSIGNMENT':
        allowed=('group_count','group_sizes','min_leaders','junior_years','mentor_ids','locked','same_pairs','avoid_pairs','effective_date','weights','explicit_weight_targets','one_time_experience_ids','one_time_experience_revisions','synthetic','consent')
    else:
        allowed=('id','revision','fingerprint','experience_ids','one_time_experience_revisions','synthetic','consent')
        draft=BedDraft.objects.filter(pk=data.get('id'),group=group).first()
        if not draft: raise BusinessError('请先同步当班护士。')
        require_revision(draft,data.get('revision'))
    parameters={key:data[key] for key in allowed if key in data}
    from scheduling.services.ai_scenes import scene_snapshot
    parameters['ai_config']=scene_snapshot('group_assignment' if kind=='GROUP_ASSIGNMENT' else 'bed_assignment')
    # Keep the credential only in worker memory, never in the task JSON or logs.
    api_key=None
    if dispatch:
        from scheduling.services.ai_configuration import secret_value
        api_key=secret_value()
    try:
        with transaction.atomic():
            lock_group(group);group.refresh_from_db()
            previous=data.get('previous_task_id')
            if previous is not None:
                if type(previous) is not int:raise BusinessError('上一轮任务编号无效。')
                prior=AllocationExecution.objects.filter(pk=previous,group=group,kind=kind).first()
                if not prior:raise BusinessError('上一轮任务不存在或无权访问。',404)
                parameters['previous_task_id']=prior.id
                if prior.status in ('queued','running'):
                    if data.get('supersede_confirmed') is not True:raise BusinessError('当前任务仍在运行，请确认以新任务替代。',409)
                    AllocationExecution.objects.filter(pk=prior.pk,status__in=['queued','running']).update(status='superseded',error='已由新一轮优化替代。',completed_at=timezone.now())
                    AllocationEvent.objects.create(task=prior,stage=prior.current_stage,status='failed',summary={'message':'已由新一轮优化替代。'})
            if kind=='GROUP_ASSIGNMENT':
                from scheduling.services import grouping
                people,_=grouping.collect(group)
                if not people:raise BusinessError('没有符合条件的在岗固定轮转护士。',422)
                parameters['optimization_snapshot']=grouping.parameters(data,people,group)
                parameters['data_fingerprint']=grouping.fingerprint(group,include_preferences=False)
            else:
                from scheduling.services import beds,experiences
                draft.refresh_from_db();require_revision(draft,data.get('revision'));beds.check_context(draft,data)
                parameters['optimization_snapshot']=experiences.resolve_weights(group,experiences.BED,
                    {'load_balance':1.0,'minimize_three':0.5,'continuity':0.1,'assessment_match':0.0},data.get('experience_ids',[]),(),data.get('one_time_experience_revisions'))
            task=AllocationExecution.objects.create(group=group,created_by=user,kind=kind,parameters=parameters)
            audit(user,group,'创建分配优化任务',{'task':task.id,'kind':kind,'previous_task_id':previous})
            if dispatch:
                transaction.on_commit(lambda: Thread(target=run,args=(task.pk,api_key),daemon=True,name='icu-allocation').start())
    except IntegrityError:
        raise BusinessError('该业务已有任务正在执行，请查看当前任务。',409)
    return task


def expire_stale(group,kind):
    """Mark tasks without any progress for three minutes interrupted on access.

    This enables safe retry after process restart; it does not claim to resume
    an in-process solver. The old worker cannot commit a result after expiry.
    """
    for task in AllocationExecution.objects.filter(group=group,kind=kind,status__in=['queued','running']):
        cutoff=timezone.now()-timedelta(seconds=max(180,task.parameters.get('ai_config',{}).get('timeout_seconds',30)+60))
        if task.created_at>=cutoff: continue
        with transaction.atomic():
            # Acquire SQLite's write reservation before checking the latest event.
            active=AllocationExecution.objects.filter(pk=task.pk,status__in=['queued','running'])
            if not active.update(current_stage=F('current_stage')): continue
            last=task.events.order_by('-id').first()
            if last and last.created_at>=cutoff: continue
            message='任务长时间无进展，可能因服务重启中断；请重新生成。'
            task.refresh_from_db()
            active.update(status='failed',error=message,completed_at=timezone.now())
            AllocationEvent.objects.create(task=task,stage=task.current_stage,status='failed',summary={'message':message})


def _bed_ai(draft,nurses,patients,task,api_key=None):
    from scheduling.services import beds
    from scheduling.services.ai_configuration import chat_completion, hospital_authorized, public_config, secret_value
    if any(not p.synthetic for p in patients) and (task.parameters.get('synthetic') or not hospital_authorized()):
        raise BusinessError('真实患者资料尚未获得外发授权或资料来源标记不正确。',403)
    if not task.parameters['synthetic'] and os.getenv('AI_PERSONNEL_EXTERNAL_AUTHORIZED','false').lower()!='true':
        raise BusinessError('真实人员资料尚未获得服务器端外发授权。',403)
    config=public_config()
    if not config['configured']: raise BusinessError('AI服务未配置。',503)
    # Only aggregate counts and qualification match counts; no patient narratives,
    # names, bed numbers, identifiers or free-text staff information leave the host.
    loads=dict(Counter(p.load for p in patients))
    matches=[sum(beds.qualified(n,p,draft.date,draft.group.rules) for n in nurses) for p in patients]
    checked_stage=task.events.filter(stage=3,status='completed').order_by('-id').first()
    applied_weights=checked_stage.summary.get('weights',{}) if checked_stage else {}
    payload={'patients':len(patients),'nurses':len(nurses),'load_counts':loads,
             'qualified_nurse_counts':sorted(matches),'max_beds':draft.group.rules['max_beds'],
             'max_load':draft.group.rules['max_load'],
             'assessment_level_counts':dict(Counter(str(n.assessment_level) if n.assessment_level else '未记录' for n in nurses)),
             'reviewed_assessment_need_counts':dict(Counter((p.assessment_need or '待核对') if p.reviewed_by_id else '待核对' for p in patients)),
             'optimization_weights':applied_weights}
    snapshot=task.parameters.get('ai_config')
    event(task,4,'running',{'model':snapshot['model'] if snapshot else config['model'],'message':'正在分析已核对的汇总数据。'})
    text=chat_completion(config,api_key if api_key is not None else secret_value(),[
        {'role':'system','content':'根据给定汇总数据简要说明床位匹配重点和可能不足，不得编造患者、护士或资质。评估能级1最高，评估需求由人工确认，不能从护理负荷推断。数据不含任何患者床号或身份，匹配人数是匿名排序，不能与患者负荷、床号或个体对应；不得推断某位患者具有哪种资格匹配。返回 JSON 对象，summary 为不超过300字的字符串；可选optimization_suggestions数组最多5项，每项包含title、description、target、weight，target只能为load_balance、minimize_three、continuity、assessment_match；权重为0到1且步长0.1，无法映射时target留空，建议须人工确认。无明确建议则返回空数组。'},
        {'role':'user','content':json.dumps(payload,ensure_ascii=False)}],
        scene='bed_assignment',scene_config=snapshot)
    try:
        response=json.loads(text);summary=response['summary']
        if not isinstance(summary,str) or not 1<=len(summary)<=800: raise ValueError()
    except (ValueError,TypeError,KeyError): raise BusinessError('AI床位分析结果格式无效，未生成方案。',502)
    from scheduling.services.experiences import save_task_suggestions,BED
    suggestions,rejected=save_task_suggestions(draft.group,task.created_by,BED,response.get('optimization_suggestions',[]),
        snapshot['model'] if snapshot else config['model'],task.id,payload['optimization_weights'],
        lambda:AllocationExecution.objects.filter(pk=task.id,status='running').exists(),
        {'patients':len(patients),'nurses':len(nurses),'load_counts':loads,
         'assessment_level_counts':payload['assessment_level_counts'],'assessment_need_counts':payload['reviewed_assessment_need_counts']})
    event(task,4,'completed',{'model':snapshot['model'] if snapshot else config['model'],'summary':summary,'review_required':True,
        'pending_suggestions':suggestions,'invalid_suggestions':rejected})


def run(task_id,api_key=None):
    close_old_connections()
    task=None
    try:
        task=AllocationExecution.objects.select_related('group','created_by').get(pk=task_id)
        if not AllocationExecution.objects.filter(pk=task_id,status='queued').update(status='running',started_at=timezone.now()): return
        scope(task.created_by,task.group_id,write=True)
        progress=lambda stage,status,summary=None:event(task,stage,status,summary)
        if task.kind=='GROUP_ASSIGNMENT':
            from scheduling.services import grouping
            result=grouping.create_task(task.group,task.created_by,task.parameters,progress=progress,scene_config=task.parameters.get('ai_config'),api_key=api_key,
                optimization_snapshot=task.parameters.get('optimization_snapshot'),expected_fingerprint=task.parameters.get('data_fingerprint'),
                still_active=lambda:AllocationExecution.objects.filter(pk=task_id,status='running').exists(),execution_id=task.id)
            result={'grouping_task_id':result.pk}
        else:
            from scheduling.services import beds
            draft=BedDraft.objects.get(pk=task.parameters['id'],group=task.group)
            require_revision(draft,task.parameters.get('revision'))
            beds.check_context(draft,task.parameters)
            result=beds.recommend(draft,progress=progress,before_solve=lambda d,n,p:_bed_ai(d,n,p,task,api_key=api_key),experience_ids=task.parameters.get('experience_ids',[]),preference_snapshot=task.parameters.get('optimization_snapshot'))
        AllocationExecution.objects.filter(pk=task_id,status='running').update(status='completed',result=result,completed_at=timezone.now())
    except Exception as exc:
        if task:
            if isinstance(exc,BusinessError):
                detail=exc.detail
                message=str(detail.get('message','任务失败。')) if isinstance(detail,dict) else str(detail)
            else: message='任务执行失败，请检查服务日志和配置后重试。'
            message=message[:500]
            stage=AllocationExecution.objects.filter(pk=task_id).values_list('current_stage',flat=True).first() or 0
            if AllocationExecution.objects.filter(pk=task_id,status__in=['queued','running']).exists():
                event(task,stage,'failed',{'message':message})
                AllocationExecution.objects.filter(pk=task_id,status__in=['queued','running']).update(status='failed',error=message,completed_at=timezone.now())
    finally:
        close_old_connections()
