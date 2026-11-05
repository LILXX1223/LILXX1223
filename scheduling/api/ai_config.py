from rest_framework.decorators import api_view
from rest_framework.exceptions import PermissionDenied
from scheduling.api.core import ok
from scheduling.services.ai_configuration import public_config, save_config, test_connection
from scheduling.services.common import role_of
from scheduling.services.ai_scenes import list_scenes, save_scene, scene_snapshot


@api_view(['GET','PATCH'])
def configuration(request):
    role=role_of(request.user)
    if role not in ['system_admin','group_manager','head_nurse']:
        raise PermissionDenied('当前角色无权查看AI服务配置。')
    if request.method=='GET': return ok(public_config())
    if role!='system_admin': raise PermissionDenied('仅系统管理员可以修改AI服务配置。')
    return ok(save_config(request.user,request.data))


@api_view(['POST'])
def test_service(request):
    if role_of(request.user)!='system_admin': raise PermissionDenied('仅系统管理员可以测试AI服务连接。')
    return ok(test_connection(request.data))


@api_view(['GET','PATCH'])
def scenarios(request):
    role=role_of(request.user)
    if role not in ['system_admin','group_manager','head_nurse']: raise PermissionDenied('当前角色无权查看AI业务配置。')
    if request.method=='GET': return ok(list_scenes())
    if role!='system_admin': raise PermissionDenied('仅系统管理员可以修改AI业务配置。')
    return ok(save_scene(request.user,request.data))


@api_view(['POST'])
def test_scenario(request):
    if role_of(request.user)!='system_admin': raise PermissionDenied('仅系统管理员可以测试AI业务配置。')
    from scheduling.services.ai_configuration import chat_completion, stream_completion, public_config, secret_value
    from scheduling.models import AIRequestLog
    from scheduling.services.common import BusinessError
    snapshot=scene_snapshot(request.data.get('scene'));config=public_config()
    if snapshot['scene']=='medical_image': raise BusinessError('当前页面尚无病历图片输入，图片业务测试暂不可用。',422)
    instruction='这是合成数据测试。请只返回JSON对象，示例：{"ok":true}。' if snapshot['output_format']=='json_object' else '这是合成数据测试。请只回复 OK。'
    messages=[{'role':'system','content':instruction},{'role':'user','content':'测试场景配置，不包含患者或护士资料。'}]
    info={'model':snapshot['model'],'thinking':snapshot['thinking'],
        'max_tokens':snapshot['max_tokens'],'timeout_seconds':snapshot['timeout_seconds'],'stream':snapshot['stream'],
        'output_format':snapshot['output_format']}
    try:
        if snapshot['stream']:
            chunks=list(stream_completion(config,secret_value(),messages,snapshot))
            request_id=chunks[-1][1]['request_id']
        else:
            _,request_id=chat_completion(config,secret_value(),messages,scene_config=snapshot,with_metadata=True)
    except BusinessError as exc:
        request_id=str(exc.detail.get('request_id',''));log=AIRequestLog.objects.filter(request_id=request_id).first()
        return ok({**info,'ok':False,'request_id':request_id,'error':str(exc.detail.get('message','测试失败。')),
            'error_code':str(exc.detail.get('code','configuration')),'elapsed_ms':log.elapsed_ms if log else None,
            'http_status':log.http_status if log else None,'finish_reason':log.finish_reason if log else '',
            'usage':log.usage if log else {},'json_valid':False})
    log=AIRequestLog.objects.get(request_id=request_id)
    return ok({**info,'ok':True,'request_id':request_id,'elapsed_ms':log.elapsed_ms,'http_status':log.http_status,
        'finish_reason':log.finish_reason,'usage':log.usage,'json_valid':snapshot['output_format']=='json_object'})
