"""Shared AI configuration: encrypted secrets and bounded HTTPS requests."""
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from scheduling.models import AIServiceConfiguration
from scheduling.services.common import BusinessError, audit

DEFAULTS={'provider':'deepseek','base_url':'https://api.deepseek.com','model':'deepseek-flash'}


def hospital_authorized():
    return os.getenv('AI_HOSPITAL_AUTHORIZED','false').lower()=='true'


def public_config():
    obj=AIServiceConfiguration.objects.filter(pk=1).first()
    data={k:getattr(obj,k) if obj else value for k,value in DEFAULTS.items()}
    data.update(has_key=bool(obj and obj.encrypted_key),revision=obj.revision if obj else 0,
                hospital_authorized=hospital_authorized())
    data['configured']=bool(data['has_key'] and data['model'] and data['base_url'])
    return data


def validate_settings(values):
    provider=values.get('provider');base=values.get('base_url');model=values.get('model')
    if provider not in ['deepseek','openai_compatible']: raise BusinessError('请选择支持的服务商。')
    if not isinstance(model,str) or not model.strip() or len(model)>120 or re.search(r'[\x00-\x1f\x7f]',model):
        raise BusinessError('模型名称无效。')
    if not isinstance(base,str) or len(base)>500: raise BusinessError('API地址无效。')
    try:
        parsed=urlsplit(base.strip()); host=parsed.hostname; port=parsed.port
    except ValueError: raise BusinessError('API地址无效。')
    if parsed.scheme!='https' or not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BusinessError('API地址须为不含账号、查询参数或片段的HTTPS地址。')
    if host.lower() in ['localhost','localhost.localdomain'] or host.lower().endswith('.localhost'):
        raise BusinessError('API地址不能指向本机。')
    return {'provider':provider,'base_url':base.strip().rstrip('/'),'model':model.strip()}


def _public_unicast(address):
    ip=ipaddress.ip_address(address)
    return ip.is_global and not (ip.is_multicast or ip.is_unspecified or ip.is_reserved or ip.is_loopback or ip.is_link_local)


def check_external_host(base, provider):
    """Resolve once and return a validated IP which must be used for the connection."""
    parsed=urlsplit(base);host=parsed.hostname
    try:
        addresses={ipaddress.ip_address(x[4][0].split('%')[0]) for x in
                   socket.getaddrinfo(host,parsed.port or 443,type=socket.SOCK_STREAM)}
    except (OSError,ValueError): raise BusinessError('AI服务地址无法解析，请检查网络与配置。',502)
    # Some VPNs return Fake-IP addresses in 198.18/15. Only for the fixed official
    # DeepSeek host, obtain public addresses via authenticated DNS-over-HTTPS.
    # No API key or clinical data is sent to the resolver.
    fake_range=ipaddress.ip_network('198.18.0.0/15')
    if (provider=='deepseek' and host.lower()=='api.deepseek.com' and addresses
            and all(ip.version==4 and ip in fake_range for ip in addresses)):
        try:
            response=httpx.get('https://dns.alidns.com/resolve',params={'name':'api.deepseek.com','type':'A'},
                               timeout=5,follow_redirects=False,trust_env=False)
            response.raise_for_status()
            payload=response.json()
            if payload.get('Status')!=0: raise ValueError('DNS error')
            addresses={ipaddress.ip_address(item['data']) for item in payload.get('Answer',[]) if item.get('type')==1}
        except (httpx.HTTPError,ValueError,TypeError,KeyError,AttributeError):
            raise BusinessError('DeepSeek 公网地址解析失败，请检查网络或代理 DNS 设置。',502)
    if not addresses or any(not _public_unicast(ip) for ip in addresses):
        raise BusinessError('AI服务地址不可指向内网、保留地址或本机。')
    return str(sorted(addresses,key=lambda ip:(ip.version,int(ip)))[0])


def master_key(create=False):
    configured=os.getenv('ICU_AI_ENCRYPTION_KEY','').strip()
    if configured:
        try: Fernet(configured.encode('ascii'))
        except (ValueError,UnicodeEncodeError): raise BusinessError('AI加密主密钥格式错误，请联系系统管理员。',503)
        return configured.encode('ascii')
    directory=Path.home()/'.local'/'share'/'icu-scheduler'
    target=directory/'ai-master.key'
    if create:
        directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        if directory.stat().st_mode & 0o077: raise BusinessError('AI主密钥目录权限不安全，请限制为仅当前用户访问。',503)
        try:
            fd=os.open(str(target),os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        except FileExistsError: pass
        else:
            with os.fdopen(fd,'wb') as handle: handle.write(Fernet.generate_key())
    try:
        fd=os.open(str(target),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'rb') as handle:
            info=os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid!=os.getuid():
                raise BusinessError('AI主密钥文件权限不安全，请联系系统管理员。',503)
            key=handle.read(1000).strip()
        Fernet(key)
        return key
    except BusinessError: raise
    except (OSError,ValueError): raise BusinessError('AI加密主密钥不可用，请联系系统管理员。',503)


def secret_value():
    obj=AIServiceConfiguration.objects.filter(pk=1).first()
    if not obj or not obj.encrypted_key: return ''
    try: return Fernet(master_key()).decrypt(obj.encrypted_key.encode('ascii')).decode('utf-8')
    except (InvalidToken,ValueError,UnicodeError): raise BusinessError('AI密钥无法解密，请重新配置。',503)


def clean_key(value):
    if value in [None,'']: return ''
    if not isinstance(value,str) or len(value)>2048 or not value.strip() or re.search(r'[\x00-\x1f\x7f]',value):
        raise BusinessError('API Key格式无效。')
    return value.strip()


@transaction.atomic
def save_config(user,data):
    current=public_config();revision=data.get('revision')
    if type(revision) is not int or revision!=current['revision']: raise BusinessError('AI配置已改变，请刷新后重试。',409)
    values=validate_settings({k:data.get(k,current[k]) for k in DEFAULTS})
    key=clean_key(data.get('api_key'))
    values['changed_by']=user;values['updated_at']=timezone.now()
    if key: values['encrypted_key']=Fernet(master_key(create=True)).encrypt(key.encode('utf-8')).decode('ascii')
    if revision==0:
        obj=AIServiceConfiguration.objects.create(pk=1,revision=1,**values)
    else:
        count=AIServiceConfiguration.objects.filter(pk=1,revision=revision).update(revision=F('revision')+1,**values)
        if count!=1: raise BusinessError('AI配置已改变，请刷新后重试。',409)
    audit(user,None,'更新AI服务配置',{'provider':values['provider'],'model':values['model'],'key_replaced':bool(key),'revision':revision+1})
    return public_config()


def _request_target(settings):
    clean=validate_settings(settings);address=check_external_host(clean['base_url'],clean['provider'])
    parsed=urlsplit(clean['base_url']);host=parsed.hostname.encode('idna').decode('ascii')
    host_header=f'[{host}]' if ':' in host else host
    connect_host=f'[{address}]' if ':' in address else address
    if parsed.port:
        host_header+=f':{parsed.port}';connect_host+=f':{parsed.port}'
    return clean, urlunsplit(('https',connect_host,parsed.path.rstrip('/')+'/chat/completions','','')), host_header, host


def _failure(code, status=502, request_id=None):
    messages={'auth':'DeepSeek身份验证失败，请检查服务商密钥。', 'invalid_request':'AI模型或请求参数无效，请检查场景配置。',
        'payment':'AI服务账户余额或计费状态异常，请检查服务商账户。',
        'rate_limit':'AI服务限流，请稍后重试。', 'timeout':'AI服务请求超时，请稍后重试或调整该场景超时。',
        'network':'AI服务网络连接失败，请检查网络。', 'provider_error':'AI服务返回错误，请稍后重试。',
        'empty':'AI返回内容为空，请重试。', 'truncated':'AI输出被截断，请增加该场景的输出Token后重试。',
        'invalid_json':'AI返回的JSON不完整或格式无效，请重试。', 'invalid_response':'AI响应结构无效，请重试。'}
    error=BusinessError(messages.get(code,messages['provider_error'])+(f'（请求编号 {request_id}）' if request_id else ''),status)
    error.detail['code']=code
    if request_id: error.detail['request_id']=request_id
    return error


def _error_code(exc):
    if isinstance(exc,httpx.TimeoutException): return 'timeout'
    if isinstance(exc,httpx.HTTPStatusError):
        status=exc.response.status_code
        if status in (401,403): return 'auth'
        if status==402: return 'payment'
        if status==408: return 'timeout'
        if status==429: return 'rate_limit'
        if 400<=status<500: return 'invalid_request'
        return 'provider_error'
    if isinstance(exc,httpx.HTTPError): return 'network'
    return 'invalid_response'


def _log_start(snapshot):
    if not snapshot: return None
    from scheduling.models import AIRequestLog
    return AIRequestLog.objects.create(request_id=uuid.uuid4().hex,scene=snapshot['scene'],parameters={
        k:snapshot[k] for k in ('model','thinking','max_tokens','timeout_seconds','stream','output_format','revision','service_revision','configuration_source') if k in snapshot})


def _log_finish(log,start,status,*,http_status=None,code='',finish='',usage=None):
    if not log: return
    safe_usage={k:v for k,v in (usage if isinstance(usage,dict) else {}).items() if k in ('prompt_tokens','completion_tokens','total_tokens') and type(v) is int and v>=0}
    type(log).objects.filter(pk=log.pk).update(status=status,http_status=http_status if type(http_status) is int else None,
        error_code=code,finish_reason=(finish if isinstance(finish,str) else '')[:40],elapsed_ms=max(0,int((time.monotonic()-start)*1000)),usage=safe_usage)


def chat_completion(settings,key,messages,scene=None,scene_config=None,with_metadata=False,**options):
    if not key: raise BusinessError('AI服务未配置，请由系统管理员填写API Key。',503)
    snapshot=scene_config
    if scene and not snapshot:
        from scheduling.services.ai_scenes import scene_snapshot
        snapshot=scene_snapshot(scene,settings)
    if snapshot:
        from scheduling.services.ai_scenes import request_options
        settings={**settings,**{k:snapshot[k] for k in ('provider','base_url','model')}}
        options=request_options(snapshot)
    log=_log_start(snapshot);start=time.monotonic();http_status=None;finish='';usage={}
    try:
        clean,endpoint,host_header,host=_request_target(settings)
        payload={'model':clean['model'],'messages':messages,**options}
        with httpx.Client(timeout=snapshot['timeout_seconds'] if snapshot else 30,follow_redirects=False,trust_env=False) as client:
            response=client.post(endpoint,headers={'Authorization':'Bearer '+key,'Host':host_header},
                json=payload,extensions={'sni_hostname':host})
        http_status=response.status_code
        response.raise_for_status()
        result=response.json()
        if not isinstance(result,dict) or not isinstance(result.get('choices'),list) or not result['choices']:
            raise ValueError('invalid response')
        choice=result['choices'][0];finish=choice.get('finish_reason') or '';usage=result.get('usage') or {}
        if finish=='length': raise _failure('truncated',request_id=log.request_id if log else None)
        if finish!='stop': raise _failure('invalid_response',request_id=log.request_id if log else None)
        text=choice['message']['content']
        if not isinstance(text,str) or not text.strip(): raise _failure('empty',request_id=log.request_id if log else None)
        if options.get('response_format',{}).get('type')=='json_object':
            try: parsed=json.loads(text)
            except (TypeError,ValueError): raise _failure('invalid_json',request_id=log.request_id if log else None)
            if not isinstance(parsed,dict): raise _failure('invalid_json',request_id=log.request_id if log else None)
        _log_finish(log,start,'success',http_status=http_status,finish=finish,usage=usage)
        return (text,log.request_id if log else None) if with_metadata else text
    except BusinessError as exc:
        code=str(exc.detail.get('code','endpoint_rejected'))
        if log and 'request_id' not in exc.detail:exc.detail['request_id']=log.request_id
        if 'code' not in exc.detail:exc.detail['code']=code
        _log_finish(log,start,'failed',http_status=http_status,code=code,finish=finish,usage=usage)
        raise
    except (httpx.HTTPError,ValueError,TypeError,KeyError,IndexError) as exc:
        code=_error_code(exc)
        _log_finish(log,start,'failed',http_status=http_status,code=code,finish=finish,usage=usage)
        raise _failure(code,request_id=log.request_id if log else None) from None


def stream_completion(settings,key,messages,snapshot):
    """Yield only final-answer deltas; model reasoning is never sent to the browser."""
    from scheduling.services.ai_scenes import request_options
    if not key: raise BusinessError('AI服务未配置，请由系统管理员填写API Key。',503)
    settings={**settings,**{k:snapshot[k] for k in ('provider','base_url','model')}}
    log=_log_start(snapshot);start=time.monotonic();http_status=None;finish='';usage={};has_content=False;completed=False
    try:
        clean,endpoint,host_header,host=_request_target(settings)
        payload={'model':clean['model'],'messages':messages,**request_options(snapshot),'stream':True,
                 'stream_options':{'include_usage':True}}
        with httpx.Client(timeout=snapshot['timeout_seconds'],follow_redirects=False,trust_env=False) as client:
            with client.stream('POST',endpoint,headers={'Authorization':'Bearer '+key,'Host':host_header},
                    json=payload,extensions={'sni_hostname':host}) as response:
                http_status=response.status_code;response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith('data:'): continue
                    data=line[5:].strip()
                    if data=='[DONE]': completed=True;break
                    try: item=json.loads(data)
                    except ValueError: raise _failure('invalid_response',request_id=log.request_id)
                    if not isinstance(item,dict): raise _failure('invalid_response',request_id=log.request_id)
                    if isinstance(item.get('usage'),dict):usage=item['usage']
                    for choice in item.get('choices') or []:
                        if choice.get('finish_reason'):finish=choice['finish_reason']
                        content=(choice.get('delta') or {}).get('content')
                        if isinstance(content,str) and content:
                            has_content=True;yield ('delta',content)
        if finish=='length': raise _failure('truncated',request_id=log.request_id)
        if finish!='stop': raise _failure('invalid_response',request_id=log.request_id)
        if not completed: raise _failure('invalid_response',request_id=log.request_id)
        if not has_content: raise _failure('empty',request_id=log.request_id)
        _log_finish(log,start,'success',http_status=http_status,finish=finish,usage=usage)
        yield ('done',{'request_id':log.request_id,'finish_reason':finish,'usage':usage})
    except BusinessError as exc:
        code=str(exc.detail.get('code','endpoint_rejected'))
        if log and 'request_id' not in exc.detail:exc.detail['request_id']=log.request_id
        if 'code' not in exc.detail:exc.detail['code']=code
        _log_finish(log,start,'failed',http_status=http_status,code=code,finish=finish,usage=usage)
        raise
    except (httpx.HTTPError,ValueError,TypeError,KeyError) as exc:
        code=_error_code(exc);_log_finish(log,start,'failed',http_status=http_status,code=code,finish=finish,usage=usage)
        raise _failure(code,request_id=log.request_id) from None
    except GeneratorExit:
        _log_finish(log,start,'failed',http_status=http_status,code='client_disconnected',finish=finish,usage=usage)
        raise


def test_connection(data):
    current=public_config();values=validate_settings({k:data.get(k,current[k]) for k in DEFAULTS})
    key=clean_key(data.get('api_key')) or secret_value()
    options={'max_tokens':64,'temperature':0}
    if values['provider']=='deepseek' and urlsplit(values['base_url']).hostname.lower()=='api.deepseek.com':
        options['thinking']={'type':'disabled'}
    chat_completion(values,key,[{'role':'system','content':'请只回复 OK。'},
        {'role':'user','content':'连接测试：不包含患者或护士数据。'}],**options)
    return {'ok':True,'message':'已收到所配置模型的真实响应。','model':values['model']}
