"""Versioned, non-secret settings for each AI use case."""
from django.db import transaction
from django.db.models import F
from scheduling.models import AISceneConfiguration
from scheduling.services.common import BusinessError, audit

# Values are engineering starting points, not clinical recommendations.
SCENES = {
    'medical_single': ('单床病历文字识别', 'disabled', 8192, 120, False, 'json_object'),
    'medical_batch': ('批量病历文字识别', 'disabled', 8192, 120, False, 'json_object'),
    'medical_image': ('病历图片识别（预留）', 'disabled', 8192, 120, False, 'json_object'),
    'group_assignment': ('AI护士小组分配', 'high', 16384, 180, False, 'json_object'),
    'bed_assignment': ('AI床位智能分配', 'high', 16384, 180, False, 'json_object'),
    'assistant_chat': ('AI助手常规聊天', 'low', 4096, 90, True, 'text'),
    'experience_analysis': ('AI历史经验分析', 'high', 16384, 180, False, 'json_object'),
    'excel_recognition': ('Excel字段辅助识别', 'disabled', 8192, 120, False, 'json_object'),
}


def scene_snapshot(scene, config=None):
    from scheduling.services.ai_configuration import public_config
    if not isinstance(scene,str) or scene not in SCENES: raise BusinessError('AI业务场景无效。')
    config = config or public_config()
    label, thinking, tokens, timeout, stream, output = SCENES[scene]
    row = AISceneConfiguration.objects.filter(scene=scene).first()
    return {'scene':scene, 'label':label, 'provider':config['provider'], 'base_url':config['base_url'],
        'model':row.model if row else config['model'], 'thinking':row.thinking if row else thinking,
        'max_tokens':row.max_tokens if row else tokens, 'timeout_seconds':row.timeout_seconds if row else timeout,
        'stream':row.stream if row else stream, 'output_format':row.output_format if row else output,
        'revision':row.revision if row else 0, 'service_revision':config.get('revision',0),
        'changed_by':row.changed_by.get_username() if row and row.changed_by_id else None,
        'updated_at':row.updated_at.isoformat() if row else None}


def list_scenes():
    return [scene_snapshot(scene) for scene in SCENES]


@transaction.atomic
def save_scene(user, data):
    scene=data.get('scene'); current=scene_snapshot(scene)
    if type(data.get('revision')) is not int or data['revision']!=current['revision']:
        raise BusinessError('该业务配置已变化，请刷新后重试。',409)
    model=data.get('model'); thinking=data.get('thinking'); tokens=data.get('max_tokens'); timeout=data.get('timeout_seconds')
    stream=data.get('stream'); output=data.get('output_format')
    if not isinstance(model,str) or not model.strip() or len(model)>120 or any(ord(c)<32 for c in model):
        raise BusinessError('模型名称无效。')
    if thinking not in ('disabled','low','high','max'): raise BusinessError('思考模式无效。')
    # DeepSeek's model catalog reports 393216 for deepseek-flash; other model
    # limits are provider-specific and must still be checked by the provider.
    limit=393216 if current['provider']=='deepseek' and model.strip()=='deepseek-flash' else 131072
    if type(tokens) is not int or not 128<=tokens<=limit: raise BusinessError(f'输出Token须为128至{limit}。')
    if type(timeout) is not int or not 10<=timeout<=600: raise BusinessError('请求超时须为10至600秒。')
    if type(stream) is not bool: raise BusinessError('流式输出设置无效。')
    if scene=='assistant_chat':
        if output!='text': raise BusinessError('助手聊天须使用文本输出。')
    elif output!='json_object' or stream:
        raise BusinessError('该业务需要非流式JSON结果以完成结构化校验。')
    values={'model':model.strip(),'thinking':thinking,'max_tokens':tokens,'timeout_seconds':timeout,
            'stream':stream,'output_format':output,'changed_by':user}
    if current['revision']==0:
        try: AISceneConfiguration.objects.create(scene=scene,**values)
        except Exception as exc:
            from django.db import IntegrityError
            if isinstance(exc,IntegrityError): raise BusinessError('该业务配置已变化，请刷新后重试。',409)
            raise
    else:
        from django.utils import timezone
        count=AISceneConfiguration.objects.filter(scene=scene,revision=current['revision']).update(
            revision=F('revision')+1,updated_at=timezone.now(),**values)
        if not count: raise BusinessError('该业务配置已变化，请刷新后重试。',409)
    result=scene_snapshot(scene)
    audit(user,None,'更新AI业务场景配置',{'scene':scene,'revision':result['revision'],'model':result['model'],
        'thinking':thinking,'max_tokens':tokens,'timeout_seconds':timeout,'stream':stream,'output_format':output})
    return result


def request_options(snapshot):
    options={'max_tokens':snapshot['max_tokens'], 'thinking':{'type':'disabled' if snapshot['thinking']=='disabled' else 'enabled'}}
    if snapshot['thinking']!='disabled': options['reasoning_effort']=snapshot['thinking']
    if snapshot['output_format']=='json_object': options['response_format']={'type':'json_object'}
    return options
