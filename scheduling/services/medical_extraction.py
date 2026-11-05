"""Shared, individually authenticated medical extraction operations."""
import hashlib
import secrets

from django.core import signing
from django.db import IntegrityError, transaction
from scheduling.models import Bed, Extraction, Patient
from scheduling.services import ai, bed_configuration
from scheduling.services.ai_scenes import scene_snapshot
from scheduling.services.common import BusinessError, audit


def prepare(group, user, data, parameter_source=None):
    if type(data.get('synthetic')) is not bool:
        raise BusinessError('请明确病例是否为合成数据。')
    if type(data.get('include_empty_beds', False)) is not bool:
        raise BusinessError('空床导入选项无效。')
    if data.get('include_empty_beds') and not data['synthetic']:
        raise BusinessError('空床批量导入仅支持明确选择的合成病例。', 403)
    mode = data.get('mode', 'single')
    segments = ai.split_records(data.get('text'), mode, data.get('bed_number'))
    scene = 'medical_batch' if mode == 'batch' else 'medical_single'
    if parameter_source is not None and parameter_source not in ('medical_single', 'medical_batch'):
        raise BusinessError('本批次识别参数来源无效。')
    source = parameter_source or scene
    snapshot = scene_snapshot(source)
    # Keep the real business scene visible in diagnostics while preserving the source revision.
    if source != scene:
        snapshot = {**snapshot, 'scene': scene, 'configuration_source': source}
    items, skipped = [], []
    for number, text in segments:
        bed = Bed.objects.filter(group=group, number=number, enabled=True, configured=True, visible=True).first()
        if not bed:
            raise BusinessError(f'{number}床不存在或已停用。')
        patient = Patient.objects.filter(bed=bed).first()
        if mode == 'batch' and not patient and not data.get('include_empty_beds', False):
            skipped.append({'bed_number': number, 'reason': '空床未选择导入合成病例'})
            continue
        if patient and not patient.synthetic and data['synthetic']:
            raise BusinessError(f'{number}床为真实患者，不能标记为合成病例发送；请核对资料来源及院方授权。', 403)
        token = signing.dumps({'group': group.pk, 'actor': user.pk, 'bed': number, 'bed_id': bed.pk,
            'synthetic': data['synthetic'], 'sha256': hashlib.sha256(text.encode()).hexdigest(),
            'nonce': secrets.token_hex(16), 'config': snapshot,
            'occupant_id': patient.id if patient else None, 'occupant_revision': patient.revision if patient else None,
            'checked_occupancy': True}, salt='icu-extraction')
        items.append({'bed_number': number, 'bed_id': bed.pk, 'text': text, 'token': token})
    if len(items) > 40:
        raise BusinessError('每次最多识别40个有效病例，请分批处理。')
    return {'records': items, 'skipped': skipped, 'ai_parameters': snapshot}


def _current_bed(group, token):
    filters = {'group': group, 'number': token['bed'], 'enabled': True, 'configured': True, 'visible': True}
    if token.get('bed_id') is not None:
        filters['id'] = token['bed_id']
    bed = Bed.objects.filter(**filters).first()
    if not bed:
        raise BusinessError(f"{token['bed']}床不存在、床号已变化或已停用。", 409)
    patient = Patient.objects.filter(bed=bed).first()
    if (not token.get('checked_occupancy') or token.get('occupant_id') != (patient.id if patient else None)
            or token.get('occupant_revision') != (patient.revision if patient else None)):
        raise BusinessError(f'{bed.number}床患者或资料已变化，请重新准备识别。', 409)
    if patient and not patient.synthetic and token['synthetic']:
        raise BusinessError(f'{bed.number}床为真实患者，不能标记为合成病例发送。', 403)
    return bed, patient


def execute(group, user, text, signed_token):
    try:
        token = signing.loads(signed_token, salt='icu-extraction', max_age=7200)
    except (signing.BadSignature, TypeError):
        raise BusinessError('识别任务已过期或无效，请重新开始。', 409)
    if (not isinstance(text, str) or token.get('group') != group.pk or token.get('actor') != user.pk
            or hashlib.sha256(text.encode()).hexdigest() != token.get('sha256')):
        raise BusinessError('病历内容或识别范围已变化，请重新开始。', 409)
    existing = Extraction.objects.filter(group=group, created_by=user, request_token_id=token['nonce']).first()
    if existing:
        return existing
    _current_bed(group, token)
    result, mode, metadata = ai.extract(text, token['synthetic'], scene=token['config']['scene'],
                                        scene_config=token['config'], return_metadata=True)
    # Recheck after the network request: an occupant may have changed while the model ran.
    try:
        with transaction.atomic():
            bed, patient = _current_bed(group, token)
            snapshots = [{'bed_number': bed_configuration.public_number(bed.number), 'bed_id': bed.id,
                'patient_id': patient.id if patient else None, 'patient_revision': patient.revision if patient else None,
                'fields': result}]
            extraction = Extraction.objects.create(group=group, result=snapshots, mode=mode, created_by=user,
                ai_parameters={**token['config'], **metadata}, request_token_id=token['nonce'])
            audit(user, group, '病历结构化提取', {'extraction': extraction.id, 'mode': mode, 'bed_numbers': [bed.number]})
            return extraction
    except IntegrityError:
        existing = Extraction.objects.filter(group=group, created_by=user, request_token_id=token['nonce']).first()
        if existing:
            return existing
        raise
