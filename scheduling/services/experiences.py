"""Explicitly approved soft preferences; never alter clinical hard constraints."""
import json
import os
import re
from collections import Counter
from decimal import Decimal, InvalidOperation

from django.db import transaction
from scheduling.experience_models import AllocationExperience, AllocationFeedback
from scheduling.services.ai_configuration import chat_completion, public_config, secret_value
from scheduling.services.ai_scenes import scene_snapshot
from scheduling.services.common import BusinessError, audit, lock_group, require_revision

GROUP='GROUP_ASSIGNMENT'
BED='BED_ASSIGNMENT'
TARGETS={GROUP:{'level_balance','years_balance','skill_coverage','mentoring_support','stability','assessment_balance','reference_only'},
         BED:{'load_balance','minimize_three','continuity','assessment_match','reference_only'}}
FEEDBACK_REASONS={
    GROUP:{'当前分组不合理','希望重新进行智能分组','人员配置发生变化','技能或带教搭配需要调整','临时管理需要','其他原因',
           '层级配置不合理','ICU年资搭配不合理','专科技能覆盖不足','低年资护士集中','带教支持不足','带组老师配置不合理','希望保留既有人员组合','临时人员因素'},
    BED:{'特殊护理资格不匹配','工作负荷过高','重症患者配置不合理','患者病情变化','护士临时不可用','护理连续性考虑','其他原因',
         '特殊治疗资格不匹配','护理负荷过高','高护理负荷患者集中','护理连续性需要','护士专科技能更适合其他患者'},
}


def validate_type(value):
    if value not in TARGETS: raise BusinessError('经验业务类型无效。')
    return value


def validate_fields(data):
    kind=validate_type(data.get('business_type'));title=data.get('title');description=data.get('description');target=data.get('target')
    if not isinstance(title,str) or not title.strip() or len(title)>160: raise BusinessError('经验名称须为1至160个字符。')
    if not isinstance(description,str) or not description.strip() or len(description)>1000: raise BusinessError('经验说明须为1至1000个字符。')
    if target not in TARGETS[kind]: raise BusinessError('该经验无法转换为当前业务支持的软优化目标，不能自动应用。')
    value=data.get('weight')
    if type(value) not in [int,float,str,Decimal]: raise BusinessError('权重须为0.0至1.0，步长0.1。')
    try: weight=Decimal(str(value))
    except InvalidOperation: raise BusinessError('权重须为0.0至1.0，步长0.1。')
    if not weight.is_finite() or not Decimal('0')<=weight<=Decimal('1') or weight*10!=(weight*10).to_integral_value():
        raise BusinessError('权重须为0.0至1.0，步长0.1。')
    if target=='reference_only' and weight!=0:raise BusinessError('仅供参考的建议不能设置优化权重。')
    return {'business_type':kind,'title':title.strip(),'description':description.strip(),'target':target,'weight':weight}


def serialize(item):
    return {'id':item.id,'group':item.group_id,'business_type':item.business_type,'title':item.title,
        'description':item.description,'target':item.target,'weight':float(item.weight),'status':item.status,
        'source':item.source,'source_model':item.source_model,'source_details':item.source_details,'created_by':item.created_by.username,
        'reviewed_by':item.reviewed_by.username if item.reviewed_by else None,'revision':item.revision,
        'created_at':item.created_at.isoformat(),'updated_at':item.updated_at.isoformat()}


def version_snapshot(item):
    return {'revision':item.revision,'business_type':item.business_type,'title':item.title,
            'description':item.description,'target':item.target,'weight':float(item.weight),'status':item.status}


def versions(group,item):
    """Read only snapshots actually recorded in immutable audit entries.

    Older audits without a full snapshot cannot be reconstructed. Their current
    state is available, and will be captured before the next change.
    """
    from scheduling.models import AuditLog
    current=version_snapshot(item)
    found={item.revision:{'revision':item.revision,'snapshot':current,'action':'当前状态',
                          'recorded_at':item.updated_at.isoformat(),'recorded_by':None}}
    logs=AuditLog.objects.filter(group=group,action__in=['新增待审核分配经验','审核分配经验','编辑分配经验','恢复分配经验版本'],
                                 details__id=item.id).select_related('actor').order_by('-id')
    for log in logs:
        for key in ('after','before'):
            snap=log.details.get(key)
            if not isinstance(snap,dict) or snap.get('business_type')!=item.business_type: continue
            rev=snap.get('revision')
            if type(rev) is not int or rev<1 or rev>item.revision or rev in found: continue
            found[rev]={'revision':rev,'snapshot':snap,'action':log.action,
                        'recorded_at':log.created_at.isoformat(),'recorded_by':log.actor.username}
    return sorted(found.values(),key=lambda version:version['revision'],reverse=True)


@transaction.atomic
def create(group,user,data,source='manual',source_model='',source_details=None):
    values=validate_fields(data)
    item=AllocationExperience.objects.create(group=group,created_by=user,source=source,source_model=source_model,
                                             source_details=source_details or {},**values)
    audit(user,group,'新增待审核分配经验',{'id':item.id,'business_type':item.business_type,'target':item.target,'source':source,
                                      'after':version_snapshot(item)})
    return item


@transaction.atomic
def change_status(group,user,item,data):
    lock_group(group);item.refresh_from_db();require_revision(item,data.get('revision'))
    before=version_snapshot(item)
    action=data.get('action');states={'adopt':'active','pause':'paused','resume':'active','reject':'rejected'}
    allowed={'pending':{'adopt','reject'},'active':{'pause'},'paused':{'resume','reject'},'rejected':set()}
    if data.get('confirmed') is not True: raise BusinessError('请确认经验状态变更。')
    if action=='edit':
        values=validate_fields({**data,'business_type':item.business_type})
        for field in ('title','description','target','weight'): setattr(item,field,values[field])
        # A changed preference requires fresh human approval before affecting
        # future optimization, even when the previous revision was active.
        item.status='pending';version=None;action_name='编辑分配经验'
    elif action=='restore':
        version=data.get('version')
        if type(version) is not int or version<1 or version==item.revision: raise BusinessError('请选择可恢复的历史版本。')
        previous=next((v['snapshot'] for v in versions(group,item) if v['revision']==version),None)
        if not previous: raise BusinessError('该经验版本没有可核验的历史快照，无法恢复。',409)
        values=validate_fields(previous)
        if values['business_type']!=item.business_type or previous.get('status') not in {'pending','active','paused','rejected'}:
            raise BusinessError('历史经验内容不合法，无法恢复。',409)
        for field in ('title','description','target','weight'): setattr(item,field,values[field])
        item.status=previous['status']
        action_name='恢复分配经验版本'
    else:
        if action not in allowed.get(item.status,set()): raise BusinessError('当前状态不允许该操作。',409)
        item.status=states[action]
        version=None;action_name='审核分配经验'
    item.reviewed_by=None if action=='edit' else user;item.revision+=1
    item.save(update_fields=['title','description','target','weight','status','reviewed_by','revision','updated_at'])
    audit(user,group,action_name,{'id':item.id,'business_type':item.business_type,'target':item.target,'status':item.status,
                                  'before':before,'after':version_snapshot(item),'restored_from':version})
    return item


def resolve_weights(group,kind,defaults,one_time_ids=(),explicit_targets=(),one_time_revisions=None):
    """Return transparent effective weights. Newest confirmed entry wins per target."""
    validate_type(kind)
    if not isinstance(defaults,dict) or any(k not in TARGETS[kind] for k in defaults):
        raise BusinessError('优化默认参数无效。')
    if not isinstance(one_time_ids,(list,tuple)) or any(type(i) is not int or i<1 for i in one_time_ids) or len(set(one_time_ids))!=len(one_time_ids):
        raise BusinessError('本次经验选择无效。')
    if not isinstance(explicit_targets,(list,tuple)) or any(not isinstance(k,str) or k not in defaults for k in explicit_targets) or len(set(explicit_targets))!=len(explicit_targets):
        raise BusinessError('本次明确设置的优化目标无效。')
    active=list(AllocationExperience.objects.filter(group=group,business_type=kind,status='active').order_by('-updated_at','-id'))
    chosen=list(AllocationExperience.objects.filter(group=group,business_type=kind,status='pending',id__in=one_time_ids).order_by('-updated_at','-id'))
    if len(chosen)!=len(one_time_ids): raise BusinessError('本次采用的待审核经验不存在或无权使用。',409)
    if one_time_revisions is not None:
        if not isinstance(one_time_revisions,dict) or set(one_time_revisions)!={str(i) for i in one_time_ids} or any(type(v) is not int or v<1 for v in one_time_revisions.values()):
            raise BusinessError('本次经验版本信息无效。')
        if any(one_time_revisions[str(item.id)]!=item.revision for item in chosen):
            raise BusinessError('待确认经验已变化，请重新核对后再应用。',409)
    effective=dict(defaults);applied=[];excluded=[];seen=set()
    # Explicit one-time choice overrides persistent preference, but never changes it.
    for item in chosen+active:
        if item.target not in effective:
            excluded.append({'id':item.id,'reason':'仅供AI分析参考，未形成可执行参数' if item.target=='reference_only' else '当前优化器不支持该目标'});continue
        if item.target in seen:
            excluded.append({'id':item.id,'reason':'同目标已有更新或本次指定的经验'});continue
        if item not in chosen and item.target in explicit_targets:
            excluded.append({'id':item.id,'reason':'本次明确设置的权重优先'});continue
        seen.add(item.target);effective[item.target]=float(item.weight)
        applied.append({'id':item.id,'revision':item.revision,'title':item.title,'source':item.source,
            'target':item.target,'weight':float(item.weight),'one_time':item in chosen,
            'state':'已转换为优化参数','zero_weight':float(item.weight)==0})
    return {'weights':effective,'applied':applied,'excluded':excluded}


def suggest(group,user,data):
    kind=validate_type(data.get('business_type'));text=data.get('text')
    if not isinstance(text,str) or not text.strip() or len(text)>1000: raise BusinessError('请输入不超过1000字的经验要求。')
    if data.get('consent') is not True or type(data.get('synthetic')) is not bool:
        raise BusinessError('请确认文本来源及外发授权。')
    if not data['synthetic']:
        required=['AI_PERSONNEL_EXTERNAL_AUTHORIZED']+(['AI_HOSPITAL_AUTHORIZED'] if kind==BED else [])
        if any(os.getenv(env,'false').lower()!='true' for env in required): raise BusinessError('真实资料尚未获得外发授权。',403)
    if re.search(r'(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)\d{17}[\dXx](?!\d)|[\w.+-]+@[\w.-]+',text):
        raise BusinessError('经验描述不能包含联系方式或身份证号，请先去标识化。')
    source={};context={}
    source_message=data.get('source_message_id')
    if source_message is not None:
        if not isinstance(source_message,str) or not re.fullmatch(r'[0-9a-f]{32}',source_message):raise BusinessError('来源消息编号无效。')
        from scheduling.task_models import AllocationConversation,AllocationExecution
        history=AllocationConversation.objects.filter(group=group,kind=kind).first()
        msg=next((m for m in (history.messages if history else []) if m.get('id')==source_message and m.get('role')=='assistant'),None)
        if not msg:raise BusinessError('来源消息不存在或无权访问。',404)
        if msg.get('synthetic') is not data['synthetic']:raise BusinessError('来源消息与本次声明的数据来源不一致。',403)
        # Reuse the stored pseudonymised reply, not an arbitrary client-provided
        # replacement. The user still reviews the resulting suggestion.
        text=msg.get('external_content','')
        if not isinstance(text,str) or not text or len(text)>1000:raise BusinessError('来源消息过长或缺少去标识化内容，请手动整理。')
        source={'session_id':msg.get('session_id'),'message_id':source_message,'execution_id':msg.get('execution_id')}
        execution=AllocationExecution.objects.filter(pk=msg.get('execution_id'),group=group,kind=kind,status='completed').first()
        if execution:
            from scheduling.api.allocation_assistant import _current_context
            ref={'execution_id':execution.id} if kind==BED else {'task_id':execution.result.get('grouping_task_id'),'plan_code':'A'}
            context,_=_current_context(group,kind,ref)
    config=public_config()
    if not config['configured']: raise BusinessError('AI服务未配置。',503)
    snapshot=scene_snapshot('experience_analysis',config)
    prompt=('将管理人员表达的偏好转换为一个待人工审核的软优化目标。禁止编造临床要求、资格或硬性规则。'
        '仅返回JSON对象，含title、description、target和0.0至1.0且步长0.1的weight。'
        'target只能从给定列表选择，不能确定时返回空字符串。当前事实仅供核对，不得推断缺失资格。')
    raw=chat_completion(config,secret_value(),[{'role':'system','content':prompt},
        {'role':'user','content':json.dumps({'text':text.strip(),'targets':sorted(TARGETS[kind]),'current_context':context},ensure_ascii=False)}],
        scene_config=snapshot)
    try:
        obj=json.loads(raw)
        if not isinstance(obj,dict): raise ValueError()
        if obj.get('target') not in TARGETS[kind]-{'reference_only'}:obj={**obj,'target':'reference_only','weight':0}
        values=validate_fields({**obj,'business_type':kind})
    except (ValueError,TypeError,BusinessError): raise BusinessError('AI建议无法转换为支持的软目标，请人工核对后录入。',422)
    return create(group,user,values,source='ai',source_model=snapshot['model'],source_details={'ai_parameters':snapshot,'source':source,
        'current_weights':context.get('weights',{}),'context_available':bool(context),
        'data_basis':{'active_nurses':context.get('active_nurses'),'candidate_nurses':len(context.get('nurses',[])),
                      'patient_count':context.get('patients') if isinstance(context.get('patients'),int) else None}})


def save_task_suggestions(group,user,kind,items,model,execution_id,weights,still_active=None,data_basis=None):
    """Persist only actual model-returned proposals; none affect this solve."""
    if items is None:return [],[]
    if not isinstance(items,list) or len(items)>5:return [],['AI建议列表格式无效，未保存。']
    saved=[];rejected=[]
    with transaction.atomic():
        lock_group(group)
        if still_active and not still_active():raise BusinessError('该任务已被新的任务替代。',409)
        for candidate in items:
            if not isinstance(candidate,dict):rejected.append('建议格式无效');continue
            values=dict(candidate)
            if values.get('target') not in TARGETS[kind]-{'reference_only'}:values.update(target='reference_only',weight=0)
            try:
                checked=validate_fields({**values,'business_type':kind})
                item=create(group,user,checked,source='ai',source_model=model,
                    source_details={'source':{'execution_id':execution_id,'type':'task_analysis'},'current_weights':weights,
                                    'data_basis':data_basis or {}})
                saved.append({'id':item.id,'revision':item.revision,'title':item.title,'description':item.description,
                              'target':item.target,'weight':float(item.weight),'current_weight':weights.get(item.target),
                              'executable':item.target!='reference_only','status':'pending'})
            except BusinessError:rejected.append('建议字段或权重不合法')
    return saved,rejected


def analyze_feedback(group,user,data):
    """Analyze only confirmed, allowlisted reason counts, never patient narratives."""
    kind=validate_type(data.get('business_type'))
    if data.get('consent') is not True or type(data.get('synthetic')) is not bool:
        raise BusinessError('请确认反馈资料来源及外发授权。')
    if not data['synthetic']:
        required=['AI_PERSONNEL_EXTERNAL_AUTHORIZED']+(['AI_HOSPITAL_AUTHORIZED'] if kind==BED else [])
        if any(os.getenv(env,'false').lower()!='true' for env in required):raise BusinessError('真实资料尚未获得外发授权。',403)
    records=list(AllocationFeedback.objects.filter(group=group,business_type=kind,adopted=True,
        reason__in=FEEDBACK_REASONS[kind]).order_by('-id')[:200])
    if not records: raise BusinessError('没有已采纳且原因可安全汇总的历史反馈，无法提炼经验。',422)
    counts=dict(Counter(r.reason for r in records));config=public_config()
    if not config['configured']:raise BusinessError('AI服务未配置。',503)
    snapshot=scene_snapshot('experience_analysis',config)
    instruction=('根据真实已采纳的原因频次，提出一条需人工审核的软优化建议。不可编造人员能力或临床事实，'
        '不可修改硬性约束。仅返回 JSON 对象，含 title、description、target、weight；'
        'target 必须在给定列表内，weight 必须0.0至1.0且步长0.1；无法转换时 target 返回空字符串。')
    raw=chat_completion(config,secret_value(),[{'role':'system','content':instruction},
        {'role':'user','content':json.dumps({'reason_counts':counts,'targets':sorted(TARGETS[kind])},ensure_ascii=False)}],
        scene_config=snapshot)
    try:
        obj=json.loads(raw)
        if not isinstance(obj,dict):raise ValueError()
        values=validate_fields({**obj,'business_type':kind})
    except (ValueError,TypeError,BusinessError):raise BusinessError('AI反馈分析未形成可用的软目标，请人工核对。',422)
    return create(group,user,values,source='ai',source_model=snapshot['model'],
                  source_details={'feedback_ids':[r.id for r in records],'reason_counts':counts,'count':len(records),'ai_parameters':snapshot})


def feedback_data(item):
    return {'id':item.id,'business_type':item.business_type,'task_reference':item.task_reference,
        'before':item.before,'after':item.after,'context':item.context,'reason':item.reason,'adopted':item.adopted,
        'created_by':item.created_by.username,'created_at':item.created_at.isoformat()}


@transaction.atomic
def record_feedback(group,user,data,verified_adoption=False,context=None):
    kind=validate_type(data.get('business_type'));before=data.get('before');after=data.get('after')
    if not isinstance(before,dict) or not isinstance(after,dict) or len(before)>1000 or len(after)>1000:
        raise BusinessError('调整前后方案格式无效。')
    if any(not isinstance(k,str) or not k.isdigit() or (v is not None and (type(v) is not int or v<1))
           for mapping in [before,after] for k,v in mapping.items()):
        raise BusinessError('调整快照只能包含数字业务标识及其分组或责任护士编号。')
    from scheduling.models import Bed, Nurse
    identifiers={int(k) for mapping in [before,after] for k in mapping}
    if kind==GROUP:
        if Nurse.objects.filter(group=group,id__in=identifiers).count()!=len(identifiers):
            raise BusinessError('反馈中的护士不属于当前大组。',403)
        maximum=len(group.rules.get('phases',[]))
        if any(v is not None and v>maximum for mapping in [before,after] for v in mapping.values()):
            raise BusinessError('反馈中的小组编号超出配置范围。')
    else:
        nurses={v for mapping in [before,after] for v in mapping.values() if v is not None}
        if Bed.objects.filter(group=group,id__in=identifiers).count()!=len(identifiers) or Nurse.objects.filter(group=group,id__in=nurses).count()!=len(nurses):
            raise BusinessError('反馈中的床位或护士不属于当前大组。',403)
    reason=data.get('reason','');ref=data.get('task_reference','');adopted=data.get('adopted',False)
    if not isinstance(reason,str) or len(reason)>500 or not isinstance(ref,str) or len(ref)>80 or type(adopted) is not bool:
        raise BusinessError('反馈内容无效。')
    if adopted and not verified_adoption:
        raise BusinessError('仅实际确认或发布的调整可以标记为已采纳。',403)
    if context is not None and not isinstance(context,dict): raise BusinessError('反馈上下文格式无效。')
    item=AllocationFeedback.objects.create(group=group,business_type=kind,before=before,after=after,
        context=context or {},reason=reason.strip(),task_reference=ref,adopted=adopted,created_by=user)
    audit(user,group,'记录分配人工反馈',{'id':item.id,'business_type':kind,'adopted':adopted})
    return item
