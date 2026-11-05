import secrets
import logging
import json
from datetime import timedelta
from django.conf import settings
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction, OperationalError, IntegrityError
from django.db.models import Q
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie, csrf_protect, get_token
from django.utils.decorators import method_decorator
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView, exception_handler
from rest_framework.throttling import AnonRateThrottle
from rest_framework import exceptions as drf_exceptions
from scheduling.models import *
from scheduling.api.serializers import NurseSerializer, PatientSerializer, ShiftSerializer
from scheduling.services import schedule, beds, ai, workload
from scheduling.services import bed_configuration
from scheduling.services.common import *


def handle_exception(exc, context):
    if isinstance(exc,OperationalError): return Response({'error':{'message':'数据库忙，请稍后重试；本次操作未提交。'}},status=409)
    if isinstance(exc,IntegrityError): return Response({'error':{'message':'记录冲突或已存在，请刷新后核对。'}},status=409)
    response=exception_handler(exc,context)
    if response is not None:
        detail=response.data
        if isinstance(exc,(drf_exceptions.NotAuthenticated,drf_exceptions.AuthenticationFailed)):
            error={'code':'not_authenticated','message':'登录状态已失效，请重新登录后重试。','retryable':True}
        elif isinstance(exc,drf_exceptions.PermissionDenied):
            message=str(getattr(exc,'detail',''))
            if 'CSRF' in message.upper():
                error={'code':'csrf_failed','message':'安全校验失败，请重新登录后重试。','retryable':True}
            else:
                error={'code':'permission_denied','message':message or '当前账号没有操作权限。','retryable':False}
        elif isinstance(detail,dict) and 'message' in detail:
            error=detail
        elif isinstance(detail,dict) and 'detail' in detail:
            error={'message':str(detail['detail']),'code':getattr(detail['detail'],'code','request_error')}
        else:
            error={'message':'请求数据校验失败，请核对后重试。','code':'validation_error','issues':detail}
        request=context.get('request')
        path=getattr(request,'path','')
        if path.startswith('/api/v1/beds/extract'):
            stage='preflight' if 'extract-prepare/' in path else 'recognition'
            error.setdefault('code','request_error')
            error.setdefault('stage',stage)
            error.setdefault('request_id',secrets.token_hex(8))
            error.setdefault('retryable',error['code'] in ('not_authenticated','csrf_failed','timeout','rate_limit','network','provider_error','empty','truncated','invalid_json','invalid_response'))
            # Never log patient input, cookies, credentials or response bodies.
            logging.getLogger(__name__).warning('medical_extraction request_id=%s stage=%s status=%s code=%s',
                error['request_id'],stage,response.status_code,error['code'])
        response.data={'error':error}
    return response


def ok(data): return Response({'data':data})


def user_data(user):
    role=role_of(user); profile=getattr(user,'care_profile',None)
    return {'id':user.id,'username':user.username,'name':user.first_name or user.username,'role':role,
            'group':profile.group_id if profile else None,'nurse':profile.nurse_id if profile else None,
            'label':('第3组账号' if profile and profile.group and profile.group.name=='第3组' else profile.group.name+'账号') if role=='group_manager' else {'head_nurse':'护士长账号','system_admin':'系统管理员'}.get(role,'待授权账号')}


@ensure_csrf_cookie
def csrf(request):
    # Return the cookie secret as well as setting the normal CSRF cookie.  A
    # cross-origin static Site cannot read the API-domain cookie, so the
    # hosted-origin middleware can inject this exact value into the request's
    # cookie jar while Django still performs its normal double-submit check.
    get_token(request)
    return JsonResponse({'data': {'csrf': True, 'csrf_token': request.META.get('CSRF_COOKIE')}})


class LoginThrottle(AnonRateThrottle): rate='1000/min' if getattr(settings,'ICU_E2E_MODE',False) else '10/min'


@method_decorator(csrf_protect,name='dispatch')
class LoginView(APIView):
    permission_classes=[AllowAny]
    throttle_classes=[LoginThrottle]
    def post(self,request):
        user=authenticate(request,username=request.data.get('username'),password=request.data.get('password'))
        if not user: raise BusinessError('账号或密码错误。',400)
        if role_of(user) in ['nurse','disabled_nurse']: raise BusinessError('护士个人登录已停用，请由大组管理账号办理。',403)
        login(request,user)
        data = user_data(user)
        # The static hosted frontend may be embedded in a browser that blocks
        # cross-site session cookies.  Expose the random Django session key
        # only for the current in-memory runtime; the frontend never persists
        # it and sends it only as an Authorization header on API calls.
        data['session_token'] = request.session.session_key
        return ok(data)


@api_view(['GET','POST'])
def auth(request,action):
    if role_of(request.user) in ['nurse','disabled_nurse']: raise BusinessError('护士个人登录已停用。',403)
    if action=='me' and request.method=='GET': return ok(user_data(request.user))
    if action=='logout' and request.method=='POST': logout(request); return ok(True)
    if action=='password' and request.method=='POST':
        if not request.user.check_password(request.data.get('old_password','')): raise BusinessError('原密码不正确。')
        new=request.data.get('new_password','')
        try: validate_password(new,request.user)
        except DjangoValidationError as e: raise BusinessError('；'.join(e.messages))
        request.user.set_password(new); request.user.save(); update_session_auth_hash(request,request.user); return ok(True)
    raise BusinessError('不支持的操作。',405)


def publication_data(p, personal=None, effective=False):
    entries=schedule.effective_publication_entries(p) if effective else p.snapshot['entries']
    if personal: entries=[e for e in entries if e['nurse']==personal]
    result={'id':p.id,'start_date':str(p.start_date),'duration_days':p.duration_days,
            'end_date':str(p.start_date+timedelta(days=p.duration_days-1)),'created_at':p.created_at.isoformat(),'published_by':p.published_by.username,
            'entries':entries,'revision':p.draft_revision}
    if effective:result.update(schedule.publication_status(p))
    return result


@api_view(['GET'])
def schedule_period(request):
    """Read each date from its own currently effective formal version."""
    group=scope(request.user,request.query_params.get('group'))
    start=parse_date(request.query_params.get('start_date'))
    if start.weekday()!=0: raise BusinessError('排班起始日必须是周一。')
    entries=[]; versions=[]
    for offset in range(14):
        day=start+timedelta(days=offset)
        pub=schedule.current_publication(group,day)
        if pub:
            entries.extend(schedule.effective_publication_entries(pub,day))
            if pub.id not in versions: versions.append(pub.id)
    return ok({'start_date':str(start),'duration_days':14,'entries':entries,'publication_ids':versions})


def request_data(r):
    from scheduling.services.request_history import reason_not_deletable
    from scheduling.services.shift_requests import impact
    root=r
    while root.revises_id:
        root=root.revises
    source={'id':r.id,'nurse':r.nurse_id,'name':r.nurse_name_at_request,'employee_id':r.nurse_employee_at_request,
        'nurse_id_at_request':r.nurse_id_at_request,'date':str(r.date),'kind':r.kind,'target_shift':r.target_shift,
        'partner':r.partner_id,'partner_consented':r.partner_consented,'reason':r.reason,'decision':r.decision,
        'partner_name_at_request':r.partner_name_at_request,'partner_id_at_request':r.partner_id_at_request,
        'submitted_by':r.submitted_by.username if r.submitted_by_id else None,
        'partner_confirmed_by':r.partner_confirmed_by.username if r.partner_confirmed_by_id else None,
        'partner_confirmation_note':r.partner_confirmation_note,'invalidated_by_deletion':r.invalidated_by_deletion,
        'review_note':r.review_note,'revision':r.revision,'root_id':root.id,'version_number':r.version_number,'revises':r.revises_id,
        'superseded_by':r.superseded_by_id,'reviewer':r.reviewer.get_username() if r.reviewer_id else None,
        'reviewed_at':r.reviewed_at.isoformat() if r.reviewed_at else None,
        'withdrawn_at':r.withdrawn_at.isoformat() if r.withdrawn_at else None,
        'withdrawn_by':r.withdrawn_by.get_username() if r.withdrawn_by_id else None,
        'withdrawal_reason':r.withdrawal_reason,'is_deleted':r.is_deleted,
        'deleted_at':r.deleted_at.isoformat() if r.deleted_at else None,
        'deletable':not bool(reason_not_deletable(r)),'delete_block_reason':reason_not_deletable(r)}
    applied_draft=any(any(e.get('request_id')==r.id for e in d.entries) for d in ScheduleDraft.objects.filter(group_id=r.group_id))
    current_pub=schedule.current_publication(r.group,r.date) if r.group_id else None
    applied_pub=bool(current_pub and any(e.get('request_id')==r.id for e in schedule.effective_publication_entries(current_pub,r.date)))
    related=impact(r)
    source['applied_draft']=applied_draft;source['applied_publication']=applied_pub
    source['draft_ids']=related['draft_ids'];source['publication_ids']=related['publication_ids']
    source['status']='invalidated' if r.invalidated_by_deletion else 'withdrawn' if r.decision=='withdrawn' else 'superseded' if r.superseded_by_id else 'published' if applied_pub else 'applied' if applied_draft else r.decision
    return source


@api_view(['GET'])
def workspace(request):
    group=scope(request.user,request.query_params.get('group')); role=role_of(request.user)
    day=parse_date(request.query_params.get('date',timezone.localdate())); week=parse_date(request.query_params.get('week',day-timedelta(days=day.weekday())))
    ns=Nurse.objects.filter(group=group).order_by('subgroup','id'); requests=ShiftRequest.objects.filter(group=group,is_deleted=False).select_related('nurse','submitted_by','partner_confirmed_by')
    pubs=SchedulePublication.objects.filter(group=group).order_by('-id')
    draft=ScheduleDraft.objects.filter(group=group,start_date=week).first() if role in ['group_manager','system_admin'] else None
    if draft is None and role in ['group_manager','system_admin']:
        draft=next((item for item in ScheduleDraft.objects.filter(group=group,start_date__lte=week,
            start_date__gte=week-timedelta(days=13)).order_by('-start_date')
            if item.start_date+timedelta(days=item.duration_days-1)>=week),None)
    shift=request.query_params.get('shift','A')
    if shift not in ['A','DAY','N']: raise BusinessError('床位班次必须为 A、白班或 N。')
    bd=BedDraft.objects.filter(group=group,date=day,shift=shift).first()
    # GET is read-only; a missing draft is represented by revision 0 and initialized via sync.
    if bd is None: bd=BedDraft(group=group,date=day,shift=shift,revision=0)
    pub,duty=beds.on_duty(group,day,shift)
    ps={p.bed_id:p for p in beds.patients(group)}
    latest=BedPublication.objects.filter(group=group,date=day,shift=shift).order_by('-id').first()
    assignments=bd.assignments if role in ['group_manager','system_admin'] else ({str(e['bed']):e['nurse'] for e in latest.snapshot['assignments']} if latest else {})
    all_beds=list(Bed.objects.filter(group=group))
    all_beds.sort(key=lambda bed: bed_configuration.number_sort_key(bed.number))
    # Never hide an occupied bed from the management surface, even if legacy
    # records contain inconsistent visibility/configuration flags.
    shown=[b for b in all_beds if (b.configured and b.visible) or b.id in ps]
    board={'id':bd.id,'revision':bd.revision,'date':str(day),'shift':shift,'fingerprint':beds.fingerprint(bd),'roster':bd.roster,
           'source_publication':pub.id if pub else None,'source_status':schedule.publication_status(pub)['status'] if pub else None,
           'synced_publication':bd.schedule_publication_id,'source_cancelled_since_sync':beds.cancellation_after_sync(bd,pub),
           'nurses':NurseSerializer(duty,many=True).data,'assignments':assignments,'published':latest.id if latest else None,
           'beds':[{'id':b.id,'number':bed_configuration.public_number(b.number),'enabled':b.enabled,
                    'configured':b.configured,'visible':b.visible,
                    'patient':PatientSerializer(ps[b.id]).data if b.id in ps else None} for b in shown],
           'statistics':{'configured':sum(b.configured for b in all_beds),
                         'visible':sum(b.configured and b.visible for b in all_beds),
                         'open':sum(b.enabled for b in all_beds), 'occupied':len(ps),
                         'open_not_shown':sum(b.enabled and not (b.configured and b.visible) for b in all_beds),
                         'empty':sum(b.enabled and b.id not in ps for b in all_beds)}}
    latest_week=schedule.current_publication(group,week)
    data={'user':user_data(request.user),'group':{'id':group.id,'name':group.name,'revision':group.revision,'rules':group.rules},
        'groups':list(CareGroup.objects.values('id','name')) if role in ['head_nurse','system_admin'] else [{'id':group.id,'name':group.name}],
        'nurses':NurseSerializer(ns,many=True).data,'shifts':ShiftSerializer(schedule.effective_shifts(group,day).values(),many=True).data,
        'draft':schedule.draft_data(draft) if draft else None,'publications':[publication_data(p) for p in pubs[:30]],
        'published_week':publication_data(latest_week,effective=True) if latest_week else None,
        'requests':[request_data(r) for r in requests.order_by('-id')[:100]],
        'pending_request_count':requests.filter(decision='pending',invalidated_by_deletion=False).count(),'board':board,
        'bed_configuration':bed_configuration.configuration_data(group),
        'notifications':list(Notification.objects.filter(user=request.user).order_by('-id').values()[:30]),
        'logs':list(AuditLog.objects.filter(group=group).select_related('actor').order_by('-id').values('id','action','created_at','actor__username','details')[:80]) ,
        'ai':ai.ai_status(),'synthetic_demo':not Patient.objects.filter(group=group,bed__isnull=False,synthetic=False).exists(),
        'today':str(timezone.localdate()),'server_time':timezone.now().isoformat()}
    return ok(data)


@api_view(['POST','PATCH','DELETE'])
@transaction.atomic
def nurse_write(request,pk=None):
    group=scope(request.user,request.data.get('group'),write=True)
    obj=get_object_or_404(Nurse,pk=pk,group=group) if pk else None
    if request.method=='DELETE':
        if obj is None: raise BusinessError('请选择需要删除的护士。')
        from scheduling.services.nurse_deletion import delete_nurse
        return ok(delete_nurse(group,request.user,obj,request.data.get('revision')))
    lock_group(group)
    if obj: require_revision(obj,request.data.get('revision'))
    serializer=NurseSerializer(obj,data=request.data,partial=bool(pk),context={'group':group}); serializer.is_valid(raise_exception=True)
    before=NurseSerializer(obj).data if obj else None
    levels=serializer.validated_data.pop('skill_levels',None)
    nurse=serializer.save(group=group,revision=obj.revision+1 if obj else 1)
    from scheduling.services.skills import sync_nurse_skills
    sync_nurse_skills(nurse,levels)
    after=NurseSerializer(nurse).data
    roster_keys=['status','effective_from','qualifications','base_type','workdays','subgroup','special_restrictions']
    bed_keys=roster_keys+['certifications','years','can_manage_beds']
    invalidate_drafts(group,schedule=before is None or any(before[k]!=after[k] for k in roster_keys),
        bed=before is None or any(before[k]!=after[k] for k in bed_keys))
    audit(request.user,group,'维护护士档案',{'nurse':nurse.id,'before':before,'after':NurseSerializer(nurse).data})
    return ok(NurseSerializer(nurse).data)


@api_view(['POST'])
def schedules(request,action):
    group=scope(request.user,request.data.get('group'),write=True); data=request.data
    if action in ['cancel-preview','cancel']:
        from scheduling.services import schedule_cancellation
        if action=='cancel-preview':
            result=schedule_cancellation.preview(group,data);result.pop('_snapshot',None);return ok(result)
        return ok(schedule_cancellation.cancel(group,request.user,data))
    if action=='generate':
        draft=schedule.generate(group,request.user,data.get('start_date'),data.get('include_requests',True),data.get('rotation_only',False),data.get('revision'),data.get('duration_days',7))
        return ok({'draft':schedule.draft_data(draft),'validation':schedule.validate(draft)})
    if action=='generate-stream':
        # The stream reports actual preflight reads, generation/save and
        # validation. It does not claim that a rule-based calculation is a
        # solver or synthesize intermediate progress.
        actor=request.user
        def event(kind,**payload):
            return 'data: '+json.dumps({'type':kind,**payload},ensure_ascii=False,default=str)+'\n\n'
        def events():
            from scheduling.services import shift_requests
            try:
                start=parse_date(data.get('start_date'));length=data.get('duration_days',7)
                if type(length) is not int or length not in (7,14):raise BusinessError('排班周期只能为7天或14天。')
                yield event('delta',text='正在读取护士资料。')
                count=Nurse.objects.filter(group=group,status='active').count()
                yield event('delta',text=f'已读取 {count} 名在岗护士，正在读取排班规则。')
                group.refresh_from_db(fields=['rules']);rules=group.rules
                if not isinstance(rules,dict):raise BusinessError('排班规则无效。')
                yield event('delta',text='正在加载周期内有效调班申请。')
                count=shift_requests.effective_requests(group,start,start+timedelta(days=length-1)).count()
                choice='纳入' if data.get('include_requests',True) else '本次未选择纳入'
                yield event('delta',text=f'已读取 {count} 条有效已批准申请（{choice}），正在按轮转规则计算并保存草稿。')
                draft=schedule.generate(group,actor,start,data.get('include_requests',True),data.get('rotation_only',False),data.get('revision'),length)
                yield event('delta',text='草稿已保存，正在计算完整周期的校验结果。')
                result={'draft':schedule.draft_data(draft),'validation':schedule.validate(draft)}
                yield event('done',data=result)
            except BusinessError as exc:
                yield event('error',message=str(exc.detail.get('message','排班生成失败。')))
            except (OperationalError,IntegrityError):
                yield event('error',message='数据库忙或记录冲突，请刷新并核对草稿状态。')
            except Exception:
                logging.getLogger(__name__).exception('schedule_generation_failed group=%s',group.pk)
                yield event('error',message='排班生成或校验失败，请刷新并核对草稿状态。')
        response=StreamingHttpResponse(events(),content_type='text/event-stream; charset=utf-8')
        response['Cache-Control']='no-cache, no-transform';response['X-Accel-Buffering']='no'
        return response
    draft=get_object_or_404(ScheduleDraft,id=data.get('id'),group=group)
    if action=='validate':
        require_revision(draft,data.get('revision'))
        return ok(schedule.validate(draft))
    if action=='edit':
        draft=schedule.edit(draft,request.user,data); return ok({'draft':schedule.draft_data(draft),'validation':schedule.validate(draft)})
    if action=='save':
        require_revision(draft,data.get('revision')); return ok(schedule.draft_data(draft))
    if action=='publish': return ok(publication_data(schedule.publish(draft,request.user,data)))
    raise BusinessError('未知排班操作。')


@api_view(['POST'])
@transaction.atomic
def shift_write(request):
    group=scope(request.user,request.data.get('group'),write=True); lock_group(group)
    serializer=ShiftSerializer(data=request.data); serializer.is_valid(raise_exception=True)
    code=serializer.validated_data['code']; effective=serializer.validated_data['effective_from']
    for pub in SchedulePublication.objects.filter(group=group):
        if any(e['shift']==code and e['date']>=str(effective) for e in pub.snapshot['entries']):
            raise BusinessError('该生效日期已存在正式排班，请选择后续生效日期。')
    serializer.save(group=group)
    invalidate_drafts(group,bed=False)
    audit(request.user,group,'新增班次有效版本',{'code':code,'effective_from':str(effective)})
    return ok(True)


@api_view(['POST'])
@transaction.atomic
def requests_write(request,action,pk=None):
    from scheduling.services import shift_requests
    group=scope(request.user,request.data.get('group'),write=True)
    lock_group(group)
    if action=='create':
        fields=shift_requests.validate_fields(group,request.data)
        shift_requests.check_conflicts(group,fields)
        if ShiftRequest.objects.filter(group=group,nurse=fields['nurse'],date=fields['date'],decision='pending',is_deleted=False).exists():
            raise BusinessError('该护士当天已有待审核申请，请先处理。',409)
        obj=ShiftRequest.objects.create(group=group,submitted_by=request.user,**fields)
        shift_requests.invalidate_for_dates(group,[obj.date])
        audit(request.user,group,'代录调班申请',{'request':obj.id,'nurse':obj.nurse_id,'date':str(obj.date),'kind':obj.kind,'target_shift':obj.target_shift})
        return ok(request_data(obj))
    obj=get_object_or_404(ShiftRequest,pk=pk,group=group)
    require_revision(obj,request.data.get('revision'))
    if obj.is_deleted:raise BusinessError('已隐藏的历史申请不能继续处理，请先恢复。',409)
    if obj.invalidated_by_deletion:
        raise BusinessError('申请涉及已删除的护士，保留历史记录但不能继续处理。',409)
    if action=='edit':
        return ok(request_data(shift_requests.edit(obj,request.user,request.data)))
    if action=='withdraw':
        impact=shift_requests.withdraw(obj,request.user,request.data)
        return ok({'request':request_data(obj),'impact':impact,'published_schedule_unchanged':True})
    if action=='consent':
        if obj.kind!='swap' or obj.decision!='pending': raise BusinessError('仅待审核的换班申请可以记录对方同意。',409)
        if obj.partner_consented: raise BusinessError('该换班申请已经确认，请勿重复操作。',409)
        if request.data.get('confirmed') is not True: raise BusinessError('请确认已核实换班对方同意。')
        note=request.data.get('note','')
        if not isinstance(note,str) or len(note)>500: raise BusinessError('确认备注不能超过500字。')
        obj.partner_consented=True;obj.partner_confirmed_by=request.user;obj.partner_confirmation_note=note.strip()
    elif action=='decision':
        if obj.decision!='pending': raise BusinessError('申请已审核，不能重复修改。',409)
        decision=request.data.get('decision')
        if decision not in ['approved','rejected']: raise BusinessError('审核状态无效。')
        if obj.kind=='swap' and decision=='approved' and not obj.partner_consented: raise BusinessError('换班对方尚未确认同意。')
        if decision=='approved':
            participants=[obj.nurse_id]+([obj.partner_id] if obj.kind=='swap' and obj.partner_id else [])
            conflict=shift_requests.effective_requests(group,obj.date,obj.date).exclude(pk__in=[obj.pk,obj.revises_id]).filter(
                Q(nurse_id__in=participants)|Q(kind='swap',partner_id__in=participants))
            if conflict.exists(): raise BusinessError('申请涉及的护士当天已有获批申请，请核对双方冲突。')
            if obj.revises_id:
                previous=ShiftRequest.objects.get(pk=obj.revises_id,group=group)
                if previous.decision!='approved' or previous.superseded_by_id:
                    raise BusinessError('原批准申请状态已变化，无法审核该修订。',409)
        note=request.data.get('note','')
        if not isinstance(note,str) or len(note)>500: raise BusinessError('审核备注不能超过500字。')
        obj.decision=decision;obj.reviewer=request.user;obj.reviewed_at=timezone.now();obj.review_note=note.strip()
        if decision=='approved' and obj.revises_id:
            before=shift_requests.details(previous)
            previous.superseded_by=obj;previous.revision+=1;previous.save(update_fields=['superseded_by','revision'])
            audit(request.user,group,'批准调班申请修订',{'before':before,'replacement':obj.id,'published_schedule_unchanged':True})
        shift_requests.invalidate_for_dates(group,[obj.date, obj.revises.date if obj.revises_id else None])
    else: raise BusinessError('不支持的申请操作。')
    obj.revision+=1;obj.save()
    audit(request.user,group,'处理调班申请',{'request':obj.id,'nurse_id_at_request':obj.nurse_id_at_request,
        'decision':obj.decision,'partner_consented':obj.partner_consented,'revision':obj.revision})
    return ok(request_data(obj))


@api_view(['GET'])
def workload_view(request):
    group=scope(request.user,request.query_params.get('group')); role=role_of(request.user)
    period=request.query_params.get('period','week')
    if period not in ['week','month']: raise BusinessError('统计范围仅支持本周或本月。')
    return ok(workload.summary(group,parse_date(request.query_params.get('date',timezone.localdate())),period))


@api_view(['GET'])
def overview_data(request):
    from scheduling.services.overview import summary
    return ok(summary(scope(request.user,request.query_params.get('group'))))


@api_view(['PATCH'])
@transaction.atomic
def settings_write(request):
    group=scope(request.user,request.data.get('group'),write=True); lock_group(group)
    # lock_group increment is not a user-visible change yet.
    if request.data.get('revision')!=group.revision-1: raise BusinessError('配置已改变，请刷新后重试。',409)
    values=request.data.get('rules',{})
    if not isinstance(values,dict): raise BusinessError('配置格式无效。')
    allowed=set(default_rules())
    if set(values)-allowed: raise BusinessError('配置字段未获授权。',403)
    if role_of(request.user)!='system_admin' and set(values)&{'ward_code','clinical_thresholds_confirmed'}:
        raise BusinessError('全局标识及临床规则认证仅系统管理员可维护。',403)
    rules={**group.rules,**values}
    for key,low,high in [('min_rest_hours',0,48),('max_consecutive_days',1,31),('max_beds',2,3),('normal_beds',2,2),('max_load',1,12),('high_min_years',0,60),('triple_min_years',0,60)]:
        if type(rules[key]) is not int or not low<=rules[key]<=high: raise BusinessError(key+' 超出允许范围。')
    if rules['normal_beds']>rules['max_beds']: raise BusinessError('常规床数不能超过最多床数。')
    if not isinstance(rules['phases'],list) or len(rules['phases'])!=4 or any(type(x) is not int or x not in range(4) for x in rules['phases']): raise BusinessError('四小组相位无效。')
    parse_date(rules['anchor'])
    if not isinstance(rules['min_staff'],dict) or set(rules['min_staff'])!={'A','DAY','N'} or any(type(x) is not int or not 0<=x<=100 for x in rules['min_staff'].values()): raise BusinessError('班次最低人数配置无效。')
    for key in ['allow_three_light','allow_two_light_medium','notify_publications','clinical_thresholds_confirmed']:
        if type(rules[key]) is not bool: raise BusinessError('开关参数必须为布尔值。')
    for key in ['ward_name','ward_code']:
        if not isinstance(rules[key],str) or not 1<=len(rules[key])<=80: raise BusinessError('病区信息长度无效。')
    if rules['ward_code'].strip().casefold()!=group.rules.get('ward_code','').strip().casefold():
        bed_configuration.validate_ward_change(group,rules['ward_code'])
    before=group.rules; group.rules=rules; group.save(update_fields=['rules'])
    roster_keys=['anchor','phases','min_staff','min_rest_hours','max_consecutive_days']
    bed_keys=['normal_beds','max_beds','max_load','high_min_years','triple_min_years','allow_three_light','allow_two_light_medium']
    invalidate_drafts(group,schedule=any(before[k]!=rules[k] for k in roster_keys),bed=any(before[k]!=rules[k] for k in bed_keys))
    audit(request.user,group,'修改组配置',{'before':before,'after':rules,'revision':group.revision})
    return ok(True)


@api_view(['POST'])
def notification_read(request):
    if role_of(request.user) not in ['system_admin','group_manager','head_nurse']:
        raise BusinessError('护士个人账号已停用。',403)
    Notification.objects.filter(user=request.user).update(read=True); return ok(True)
