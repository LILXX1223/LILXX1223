from django.db import transaction
from django.core import signing
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view
from scheduling.models import Bed, Patient, BedDraft, Extraction
from scheduling.api.core import ok
from scheduling.api.serializers import PatientSerializer
from scheduling.services import beds, ai, bed_configuration
from scheduling.services.common import scope, BusinessError, lock_group, parse_date, require_revision, audit, role_of
from scheduling.services.patients import record_version
import secrets
import hashlib


def target_draft(group,data,create=False):
    if data.get('id'):
        return get_object_or_404(BedDraft,group=group,pk=data['id'])
    day=parse_date(data.get('date')); shift=data.get('shift')
    if shift not in ['A','DAY','N']: raise BusinessError('床位分配班次无效。')
    draft=BedDraft.objects.filter(group=group,date=day,shift=shift).first()
    if draft: return draft
    if not create: raise BusinessError('请先同步当班护士。')
    return BedDraft.objects.create(group=group,date=day,shift=shift,revision=0)


@api_view(['POST'])
def bed_action(request,action):
    data=request.data; group=scope(request.user,data.get('group'),write=True)
    if action=='extract-prepare':
        from scheduling.services.medical_extraction import prepare
        return ok(prepare(group,request.user,data))
    if action=='extract-prepared':
        from scheduling.services.medical_extraction import execute
        extraction=execute(group,request.user,data.get('text'),data.get('token',''))
        return ok({'id':extraction.id,'mode':extraction.mode,'records':extraction.result,'review_required':True})
    if action=='extract':
        if type(data.get('synthetic')) is not bool: raise BusinessError('请明确病例是否为合成数据。')
        if type(data.get('include_empty_beds',False)) is not bool: raise BusinessError('空床导入选项无效。')
        if data.get('include_empty_beds') and not data['synthetic']: raise BusinessError('空床批量导入仅支持合成病例。',403)
        if data.get('mode')=='batch':
            raise BusinessError('批量病历请使用逐病例任务接口，不能在单次请求内同步处理整批。',409)
        segments=ai.split_records(data.get('text'),data.get('mode','single'),data.get('bed_number'))
        from scheduling.services.ai_scenes import scene_snapshot
        scene='medical_batch' if data.get('mode')=='batch' else 'medical_single'
        snapshot=scene_snapshot(scene)
        snapshots=[];request_ids=[];skipped=[]
        # Do not hold a DB write transaction across a model API call.
        for number,text in segments:
            bed=Bed.objects.filter(group=group,number=number,enabled=True,configured=True,visible=True).first()
            if not bed: raise BusinessError(str(number)+'床不存在或已停用。')
            patient=Patient.objects.filter(bed=bed).first()
            if data.get('mode')=='batch' and not patient and not data.get('include_empty_beds',False):
                skipped.append({'bed_number':number,'reason':'空床未选择导入合成病例'});continue
            if patient and not patient.synthetic and data['synthetic']:
                raise BusinessError(f'{number}床为真实患者，不能标记为合成病例发送。',403)
            result,mode,metadata=ai.extract(text,data['synthetic'],scene=scene,scene_config=snapshot,return_metadata=True)
            if metadata.get('request_id'):request_ids.append(metadata['request_id'])
            snapshots.append({'bed_number':number,'bed_id':bed.id,'patient_id':patient.id if patient else None,
                'patient_revision':patient.revision if patient else None,'fields':result})
        if not snapshots: return ok({'id':None,'mode':'skipped','records':[],'skipped':skipped,'review_required':False})
        extraction=Extraction.objects.create(group=group,result=snapshots,mode=mode,created_by=request.user,ai_parameters={**snapshot,'request_ids':request_ids})
        audit(request.user,group,'病历结构化提取',{'extraction':extraction.id,'mode':mode,'bed_numbers':[n for n,_ in segments]})
        return ok({'id':extraction.id,'mode':mode,'records':snapshots,'skipped':skipped,'review_required':True})
    if action=='review': return review_extraction(group,request.user,data)
    if action=='review-batch':
        items=data.get('items')
        if not isinstance(items,list) or not items or len(items)>40: raise BusinessError('核对病例列表无效。')
        ids=[x.get('extraction') for x in items if isinstance(x,dict)]
        if len(ids)!=len(items) or any(type(i) is not int or i<1 for i in ids) or len(set(ids))!=len(ids): raise BusinessError('核对结果存在无效或重复记录。')
        saved=[]
        with transaction.atomic():
            for item in items:
                response=review_extraction(group,request.user,{'extraction':item['extraction'],
                    'records':item.get('records'),'confirmed':data.get('confirmed')})
                saved.extend(response.data['data']['patients'])
        return ok({'patients':saved})
    if action=='patient': return edit_patient(group,request.user,data)
    if action=='discharge': return discharge(group,request.user,data)
    if action=='configure-preview': return ok(bed_configuration.preview(group,data))
    if action=='configure': return ok(bed_configuration.save(group,request.user,data))
    if action=='sync':
        with transaction.atomic():
            lock_group(group)
            draft=target_draft(group,data,create=True)
            draft=beds.sync(draft,request.user,data)
            conflicts=[{'bed_id':int(bed_id),'nurse_id':nurse_id} for bed_id,nurse_id in draft.assignments.items()
                       if nurse_id not in draft.roster]
            return ok({'id':draft.id,'revision':draft.revision,'roster':draft.roster,'conflicts':conflicts,
                       'message':'已同步，但部分草稿责任护士不在新当班名单中，请人工调整并重新校验。' if conflicts else '已同步正式排班中的当班护士。'})
    draft=target_draft(group,data)
    if action=='validate': return ok(beds.validate(draft))
    if action=='recommend':
        require_revision(draft,data.get('revision')); beds.check_context(draft,data)
        return ok(beds.recommend(draft))
    if action in ['apply','adjust']:
        draft=beds.modify(draft,request.user,data,record_adjustment=action=='adjust')
        return ok({'revision':draft.revision,'assignments':draft.assignments,'validation':beds.validate(draft)})
    if action=='publish':
        pub=beds.publish(draft,request.user,data)
        return ok({'id':pub.id,'date':str(pub.date),'shift':pub.shift,'created_at':pub.created_at,'snapshot':pub.snapshot})
    raise BusinessError('未知床位操作。')


def invalidate_boards(group,bed_ids,remove=False):
    # Dependencies changing must force a new revision rather than returning a stale idempotent publication.
    for draft in BedDraft.objects.filter(group=group):
        if remove:
            draft.assignments={key:value for key,value in draft.assignments.items() if int(key) not in bed_ids}
        draft.revision+=1; draft.save()


@transaction.atomic
def review_extraction(group,user,data):
    lock_group(group)
    extraction=get_object_or_404(Extraction,group=group,pk=data.get('extraction'))
    if extraction.reviewed: raise BusinessError('该识别结果已经核对保存，不能重复写入。',409)
    records=data.get('records')
    if not isinstance(records,list) or not records or len(records)!=len(extraction.result): raise BusinessError('核对记录数与识别床位数不一致。')
    if data.get('confirmed') is not True: raise BusinessError('请明确确认已核对床号、患者身份、诊断与当前治疗。')
    if any(not isinstance(r,dict) for r in records): raise BusinessError('核对记录格式无效。')
    numbers=[bed_configuration.public_number(bed_configuration.normalize_number(r.get('bed_number'))) for r in records]
    if len(set(numbers))!=len(numbers): raise BusinessError('核对结果中存在重复床号。')
    original={r['bed_number']:r for r in extraction.result}; saved=[]
    for record in records:
        number=bed_configuration.public_number(bed_configuration.normalize_number(record.get('bed_number'))); before=original.get(number)
        if not before: raise BusinessError('不能将识别结果写入另一个未经核对的床号。')
        bed=get_object_or_404(Bed,group=group,id=before['bed_id'],enabled=True,configured=True,visible=True)
        patient=Patient.objects.filter(bed=bed).first()
        if (patient.id if patient else None)!=before['patient_id'] or (patient.revision if patient else None)!=before['patient_revision']:
            raise BusinessError(str(number)+'床患者或资料已改变，请重新识别并核对。',409)
        fields=record.get('fields',{})
        if not isinstance(fields,dict): raise BusinessError('患者字段必须为对象。')
        # A model extraction is not a human assessment of this separate need.
        # Maintain it only through the explicitly confirmed patient edit flow.
        fields=dict(fields);fields.pop('assessment_need',None)
        if patient and not patient.synthetic and fields.get('synthetic',False) is not False:
            raise BusinessError('不能将现有真实患者改写为合成病例。',403)
        if not fields.get('name') or fields['name']=='待核对患者' or fields.get('load') not in ['light','medium','high']:
            raise BusinessError('请填写核对后的患者名称并选择护理负荷。')
        if fields.get('synthetic',True) is not True and not ai.ai_status()['hospital_authorized']:
            raise BusinessError('开发环境仅允许合成病例。',403)
        serializer=PatientSerializer(patient,data=fields,partial=bool(patient)); serializer.is_valid(raise_exception=True)
        if patient: record_version(patient,user,'首次留痕前状态')
        kwargs={'group':group,'bed':bed,'reviewed_by':user,'reviewed_at':timezone.now(),'revision':patient.revision+1 if patient else 1}
        if not patient: kwargs['code']='SYN-'+secrets.token_hex(8)
        patient=serializer.save(**kwargs); saved.append(patient.id)
        record_version(patient,user,'AI识别后人工核对')
    extraction.reviewed=True; extraction.save(update_fields=['reviewed'])
    invalidate_boards(group,[r['bed_id'] for r in extraction.result])
    audit(user,group,'人工核对患者资料',{'extraction':extraction.id,'patients':saved,'beds':numbers,'confirmed':True})
    return ok({'patients':saved})


@transaction.atomic
def edit_patient(group,user,data):
    lock_group(group); bed=get_object_or_404(Bed,group=group,pk=data.get('bed_id'),enabled=True)
    patient=Patient.objects.filter(bed=bed).first()
    if not patient and (not bed.configured or not bed.visible):
        raise BusinessError('该床位未在当前工作台配置，不能转入患者。',409)
    if patient:
        if data.get('patient_id')!=patient.id: raise BusinessError('患者身份已改变，请刷新。',409)
        require_revision(patient,data.get('patient_revision'))
    elif data.get('patient_id'): raise BusinessError('该床患者已离床，请刷新。',409)
    if data.get('confirmed') is not True: raise BusinessError('请完成人工核对。')
    fields=data.get('fields',{})
    if not isinstance(fields,dict): raise BusinessError('患者字段必须为对象。')
    if fields.get('synthetic',True) is not True and not ai.ai_status()['hospital_authorized']: raise BusinessError('开发环境仅使用合成病例。',403)
    if not fields.get('name') or fields.get('load') not in ['light','medium','high']: raise BusinessError('请填写患者名称与护理负荷。')
    serializer=PatientSerializer(patient,data=fields,partial=bool(patient)); serializer.is_valid(raise_exception=True)
    if patient: record_version(patient,user,'首次留痕前状态')
    kwargs={'group':group,'bed':bed,'reviewed_by':user,'reviewed_at':timezone.now(),'revision':patient.revision+1 if patient else 1}
    if not patient: kwargs['code']='SYN-'+secrets.token_hex(8)
    patient=serializer.save(**kwargs); invalidate_boards(group,[bed.id])
    record_version(patient,user,'人工维护患者资料')
    # Patient text is intentionally excluded from the general audit stream.
    audit(user,group,'手工维护患者资料',{'patient':patient.id,'bed':bed.number,'revision':patient.revision})
    return ok(PatientSerializer(patient).data)


@transaction.atomic
def discharge(group,user,data):
    lock_group(group); patient=get_object_or_404(Patient,group=group,pk=data.get('patient_id'),bed__isnull=False)
    require_revision(patient,data.get('patient_revision')); bed_id=patient.bed_id
    if not data.get('reason'): raise BusinessError('请填写离床原因。')
    record_version(patient,user,'首次留痕前状态')
    patient.bed=None; patient.discharged_at=timezone.now(); patient.revision+=1; patient.save()
    record_version(patient,user,'患者离床')
    invalidate_boards(group,[bed_id],remove=True)
    audit(user,group,'患者离床',{'patient':patient.id,'bed':bed_id,'reason':data['reason']})
    return ok(True)


@api_view(['GET'])
def patient_history(request, pk):
    group=scope(request.user,request.query_params.get('group'))
    if role_of(request.user)=='nurse': raise BusinessError('患者资料版本仅限授权管理角色查看。',403)
    patient=get_object_or_404(Patient,pk=pk,group=group)
    return ok([{'revision':v.revision,'action':v.action,'actor':v.actor.username,
        'recorded_at':v.recorded_at.isoformat(),'snapshot':v.snapshot}
        for v in patient.versions.select_related('actor').all()])
