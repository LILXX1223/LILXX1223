"""Permission-scoped, persistent model conversations with explicit consent."""
import json
import os
import re
import uuid
from datetime import datetime, timezone

from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import StreamingHttpResponse
from rest_framework.decorators import api_view

from scheduling.api.core import ok
from scheduling.models import Nurse, Patient, AIGroupingTask, BedDraft
from scheduling.services.ai_configuration import chat_completion, stream_completion, hospital_authorized, public_config, secret_value
from scheduling.services.ai_scenes import scene_snapshot
from scheduling.services.common import BusinessError, scope
from scheduling.task_models import AllocationConversation, AllocationExecution


def _sessions(history):
    result=[];seen=set()
    for item in history:
        ident=item.get('session_id','legacy')
        if ident not in seen:
            seen.add(ident);result.append({'id':ident,'created_at':item.get('created_at'),'label':'历史对话' if ident=='legacy' else '对话 '+str(len(result)+1)})
    return result


def _response(history,revision,session_id=None):
    if session_id is not None and (not isinstance(session_id,str) or not re.fullmatch(r'legacy|[0-9a-f]{32}',session_id)):
        raise BusinessError('对话编号无效。')
    sessions=_sessions(history)
    selected=session_id or (sessions[-1]['id'] if sessions else 'legacy')
    if session_id and session_id not in {s['id'] for s in sessions}: raise BusinessError('对话不存在或无权查看。',404)
    return {'messages':[m for m in history if m.get('session_id','legacy')==selected and m.get('role') in ['user','assistant']],
            'revision':revision,'sessions':sessions,'session_id':selected}


def _save_history(group,kind,current,revision,messages):
    if len(messages)>1000: raise BusinessError('对话记录已达上限，请由管理员按数据管理规范处理。',409)
    try:
        with transaction.atomic():
            if revision==0: current=AllocationConversation.objects.create(group=group,kind=kind,messages=messages)
            else:
                changed=AllocationConversation.objects.filter(pk=current.pk,revision=revision).update(messages=messages,revision=F('revision')+1)
                if not changed: raise BusinessError('对话已更新，请刷新后重试。',409)
                current.refresh_from_db()
    except IntegrityError: raise BusinessError('对话已更新，请刷新后重试。',409)
    return current


def _current_context(group,kind,reference):
    """Return bounded pseudonymised facts for the selected candidate, never full notes."""
    if reference is None: reference={}
    if not isinstance(reference,dict): raise BusinessError('方案上下文格式无效。')
    identities={}; facts={'active_nurses':Nurse.objects.filter(group=group,status='active').count()}
    if kind=='GROUP_ASSIGNMENT' and reference.get('task_id'):
        if type(reference['task_id']) is not int or reference['task_id']<1: raise BusinessError('分组方案编号无效。')
        task=AIGroupingTask.objects.filter(group=group,pk=reference['task_id']).first()
        if not task: raise BusinessError('分组方案不存在或无权查看。',404)
        from scheduling.services.grouping import prompt_people
        people,codes=prompt_people(task.participants)
        for person in task.participants:
            for value in [person.get('name'),person.get('employee_id')]:
                if value: identities[value]=codes[person['id']]
        plan=next((p for p in task.plans if p['code']==reference.get('plan_code','A')),None)
        if not plan: raise BusinessError('指定的候选方案不存在。')
        facts.update(candidate_status=task.status,source='仅包含已保存的原始候选，人工微调结果未纳入',
            nurses=people,group_assignments={codes[int(k)]:v for k,v in plan['assignments'].items() if int(k) in codes},
            leaders={k:codes[v] for k,v in plan.get('leaders',{}).items() if v in codes},
            weights=plan.get('effective_weights',task.parameters.get('weights',{})),metrics=plan.get('metrics',{}),
            applied_experiences=task.parameters.get('applied_experiences',[]))
    elif kind=='BED_ASSIGNMENT':
        patients=list(Patient.objects.filter(group=group,bed__isnull=False).select_related('bed').order_by('id'))
        facts['patients']=len(patients)
        task_id=reference.get('execution_id')
        if task_id:
            if type(task_id) is not int or task_id<1: raise BusinessError('床位任务编号无效。')
            task=AllocationExecution.objects.filter(pk=task_id,group=group,kind=kind,status='completed').first()
            if not task: raise BusinessError('床位候选方案不存在或无权查看。',404)
            draft=BedDraft.objects.filter(group=group,pk=task.parameters.get('id')).first()
            if not draft: raise BusinessError('对应的床位草稿不存在。',409)
            from scheduling.services import beds
            if beds.fingerprint(draft)!=task.result.get('fingerprint') or draft.revision!=task.result.get('revision'):
                raise BusinessError('候选方案所依赖的数据已变化，请先重新生成。',409)
            _,nurses=beds.on_duty(group,draft.date,draft.shift)
            nurse_codes={n.id:'N'+str(i+1).zfill(3) for i,n in enumerate(nurses)}
            for n in nurses:
                for value in [n.name,n.employee_id]:
                    if value:identities[value]=nurse_codes[n.id]
            patient_rows=[]
            for i,p in enumerate(patients):
                code='P'+str(i+1).zfill(3)
                if p.name:identities[p.name]=code
                if p.bed:identities[str(p.bed.number)+'床']=code
                # Send only known treatment categories, not free-text clinical notes.
                treatment_categories=[name for name in ['CRRT','ECMO','机械通气','PICC','气道护理','俯卧位护理','血液净化']
                    if any(name.casefold() in str(value).casefold() for value in p.treatments)]
                patient_rows.append({'code':code,'load':p.load,'reviewed':bool(p.reviewed_by_id),'assessment_need':p.assessment_need if p.reviewed_by_id else '',
                    'confirmed_treatment_categories':treatment_categories,
                    'candidate_nurse':nurse_codes.get(task.result.get('assignments',{}).get(str(p.bed_id))),
                    'eligible_nurses':[nurse_codes[n.id] for n in nurses if beds.qualified(n,p,draft.date,group.rules)]})
            facts.update(source='已保存的候选方案，尚未发布；诊断及病历原文未提供',
                shift=draft.shift,date=str(draft.date),patients_detail=patient_rows,
                nurses=[{'code':nurse_codes[n.id],'level':n.level,'years':n.years,'assessment_level':n.assessment_level} for n in nurses],
                evaluation=task.result.get('evaluation',{}),weights=task.result.get('effective_weights',{}))
            # Evaluation nurse identifiers must also remain pseudonymous.
            for row in facts.get('evaluation',{}).get('nurses',[]):
                row['nurse_code']=nurse_codes.get(row.pop('nurse_id',None))
    return facts,identities


def _redact_names(text,identities):
    for value,code in sorted(identities.items(),key=lambda item:len(item[0]),reverse=True):text=text.replace(value,code)
    return text


@api_view(['GET','POST'])
def conversation(request):
    data=request.query_params if request.method=='GET' else request.data
    group=scope(request.user,data.get('group'),write=request.method=='POST')
    kind=data.get('kind')
    if kind not in ['GROUP_ASSIGNMENT','BED_ASSIGNMENT']: raise BusinessError('助手业务类型无效。')
    current=AllocationConversation.objects.filter(group=group,kind=kind).first()
    history=current.messages if current else [];revision=current.revision if current else 0
    if request.method=='GET': return ok(_response(history,revision,data.get('session_id')))
    if data.get('action')=='new':
        session_id=uuid.uuid4().hex
        marker={'role':'session','session_id':session_id,'created_at':datetime.now(timezone.utc).isoformat(),'actor':request.user.get_username()}
        current=_save_history(group,kind,current,revision,history+[marker])
        return ok(_response(current.messages,current.revision,session_id))
    message=data.get('message')
    if not isinstance(message,str) or not 1<=len(message.strip())<=500: raise BusinessError('请输入不超过500字的问题。')
    if re.search(r'(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)\d{17}[\dXx](?!\d)',message):
        raise BusinessError('问题中不能包含手机号或身份证号码，请删除后重试。')
    if type(data.get('synthetic')) is not bool or data.get('consent') is not True:
        raise BusinessError('请选择资料来源并确认外发授权。')
    if not data['synthetic'] and os.getenv('AI_PERSONNEL_EXTERNAL_AUTHORIZED','false').lower()!='true':
        raise BusinessError('真实人员资料尚未获得外发授权。',403)
    if kind=='BED_ASSIGNMENT':
        real=Patient.objects.filter(group=group,bed__isnull=False,synthetic=False).exists()
        if real and (data['synthetic'] or not hospital_authorized()): raise BusinessError('真实患者资料尚未获得外发授权或资料来源标记不正确。',403)
    config=public_config()
    if not config['configured']: raise BusinessError('AI服务未配置。',503)
    session_id=data.get('session_id') or (_sessions(history)[-1]['id'] if history else 'legacy')
    if not isinstance(session_id,str) or not re.fullmatch(r'legacy|[0-9a-f]{32}',session_id): raise BusinessError('对话编号无效。')
    if history and session_id not in {s['id'] for s in _sessions(history)}: raise BusinessError('对话不存在或无权查看。',404)
    session_history=[m for m in history if m.get('session_id','legacy')==session_id]
    execution_id=data.get('execution_id')
    if execution_id is not None:
        if type(execution_id) is not int or execution_id<1: raise BusinessError('任务编号无效。')
        execution=AllocationExecution.objects.filter(pk=execution_id,group=group,kind=kind).first()
        if not execution: raise BusinessError('分配任务不存在或无权查看。',404)
        reference=data.get('context') or {}
        if kind=='GROUP_ASSIGNMENT' and isinstance(reference,dict) and reference.get('task_id') and execution.result.get('grouping_task_id')!=reference['task_id']:
            raise BusinessError('当前候选方案与所选任务不一致，请重新选择。',409)
        if kind=='BED_ASSIGNMENT' and isinstance(reference,dict) and reference.get('execution_id') and execution_id!=reference['execution_id']:
            raise BusinessError('当前候选方案与所选任务不一致，请重新选择。',409)
    context,identities=_current_context(group,kind,data.get('context'))
    safe_history=[{'role':m['role'],'content':m.get('external_content',_redact_names(m['content'],identities))} for m in session_history[-12:]
                  if m.get('role') in ['user','assistant'] and m.get('synthetic') is data['synthetic']
                  and ('external_content' in m or data['synthetic'])]
    instruction=('你是ICU护士分组助手。' if kind=='GROUP_ASSIGNMENT' else '你是ICU床位分配助手。')
    instruction+='只能根据用户提供的信息和当前事实回答，缺少方案数据时明确说明；人员与患者代码仅用于当前对话，不得编造护士资格、患者资料或求解结果，建议仅供人工审核，不得声称已修改正式数据。当前事实：'+json.dumps(context,ensure_ascii=False)
    external_message=_redact_names(message.strip(),identities)
    snapshot=scene_snapshot('assistant_chat',config)
    prompt=[{'role':'system','content':instruction},*safe_history,{'role':'user','content':external_message}]
    key=secret_value()
    def save_reply(reply):
        if len(reply)>4000: raise BusinessError('AI回答超出长度限制，请重新提问。',502)
        shown_reply=reply
        for name,code in sorted(identities.items(),key=lambda item:len(item[0])):
            if not re.fullmatch(r'\d+床',name):shown_reply=re.sub(r'(?<![A-Za-z0-9])'+re.escape(code)+r'(?![A-Za-z0-9])',name,shown_reply)
        timestamp=datetime.now(timezone.utc).isoformat()
        messages=history+[{'id':uuid.uuid4().hex,'role':'user','content':message.strip(),'external_content':external_message,'created_at':timestamp,'actor':request.user.get_username(),'synthetic':data['synthetic'],'session_id':session_id,'execution_id':execution_id},
                          {'id':uuid.uuid4().hex,'role':'assistant','content':shown_reply,'external_content':reply,'created_at':datetime.now(timezone.utc).isoformat(),'model':snapshot['model'],'ai_parameters':snapshot,'synthetic':data['synthetic'],'session_id':session_id,'execution_id':execution_id}]
        saved=_save_history(group,kind,current,revision,messages)
        return _response(saved.messages,saved.revision,session_id)
    if data.get('stream') is True and snapshot['stream']:
        def events():
            chunks=[]
            try:
                for kind_event,value in stream_completion(config,key,prompt,snapshot):
                    if kind_event=='delta':
                        chunks.append(value)
                        if sum(map(len,chunks))>4000: raise BusinessError('AI回答超出长度限制，请重新提问。',502)
                        yield 'data: '+json.dumps({'type':'delta','text':value},ensure_ascii=False)+'\n\n'
                result=save_reply(''.join(chunks))
                yield 'data: '+json.dumps({'type':'done','data':result},ensure_ascii=False)+'\n\n'
            except BusinessError as exc:
                yield 'data: '+json.dumps({'type':'error','message':str(exc.detail.get('message','AI调用失败。'))},ensure_ascii=False)+'\n\n'
        response=StreamingHttpResponse(events(),content_type='text/event-stream; charset=utf-8')
        response['Cache-Control']='no-cache, no-transform';response['X-Accel-Buffering']='no'
        return response
    if snapshot['stream']:
        reply=''.join(value for event,value in stream_completion(config,key,prompt,snapshot) if event=='delta')
    else:
        reply=chat_completion(config,key,prompt,scene_config=snapshot)
    return ok(save_reply(reply))
