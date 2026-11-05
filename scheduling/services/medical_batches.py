"""Durable per-case state with a browser-driven, one-at-a-time execution queue.

No worker or process-local thread is assumed. Only encrypted, short-lived input is
staged; completed extractions use the existing review workflow.
"""
import hashlib
import secrets
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from scheduling.models import MedicalRecognitionBatch, MedicalRecognitionItem
from scheduling.services.ai_configuration import master_key
from scheduling.services.common import BusinessError, audit
from scheduling.services.medical_extraction import prepare, execute

TTL = timedelta(hours=2)


def clean_expired(group):
    now = timezone.now()
    expired = MedicalRecognitionItem.objects.filter(group=group, batch__expires_at__lte=now)
    expired.exclude(encrypted_text='').update(encrypted_text='')
    # Preserve an existing failure's original reason after its retry window closes.
    expired.filter(status='waiting').update(status='failed', error_code='expired',
        error_message='暂存病历已过期，请重新粘贴并创建识别任务。')
    # A request interrupted by server shutdown must not lock the queue forever.
    for item in MedicalRecognitionItem.objects.filter(group=group, status='running').select_related('batch'):
        grace = max(300, int(item.batch.parameters.get('timeout_seconds', 120)) + 90)
        if item.started_at and item.started_at + timedelta(seconds=grace) < now:
            MedicalRecognitionItem.objects.filter(pk=item.pk, status='running', lease=item.lease).update(
                status='failed', lease='', finished_at=now, error_code='interrupted',
                error_message='识别请求中断或响应状态未知，请核对后单独重试。')


def create(group, user, data):
    clean_expired(group)
    if data.get('mode') != 'batch':
        raise BusinessError('仅批量识别使用此任务队列。')
    plan = prepare(group, user, data, data.get('parameter_source', 'medical_single'))
    if not plan['records']:
        return None, plan['skipped']
    cipher = Fernet(master_key(create=True))
    with transaction.atomic():
        batch = MedicalRecognitionBatch.objects.create(group=group, created_by=user,
            parameters=plan['ai_parameters'], skipped=plan['skipped'], expires_at=timezone.now()+TTL)
        MedicalRecognitionItem.objects.bulk_create([
            MedicalRecognitionItem(batch=batch, group=group, bed_id=row['bed_id'], bed_number=str(row['bed_number']),
                text_digest=hashlib.sha256(row['text'].encode()).hexdigest(),
                encrypted_text=cipher.encrypt(row['text'].encode()).decode('ascii'), plan_token=row['token'])
            for row in plan['records']])
        audit(user, group, '创建批量病历识别任务', {'batch':batch.pk, 'count':len(plan['records']),
            'parameter_source':plan['ai_parameters'].get('configuration_source', 'medical_batch')})
    return batch, plan['skipped']


def serialize(batch):
    items = []
    for item in batch.items.select_related('extraction'):
        extraction = item.extraction
        items.append({'id':item.pk, 'bed_id':item.bed_id, 'bed_number':item.bed_number,
            'status':item.status, 'attempt':item.attempt, 'started_at':item.started_at,
            'finished_at':item.finished_at, 'error_code':item.error_code, 'error':item.error_message,
            'request_id':item.request_id, 'retryable':bool(item.encrypted_text and item.status == 'failed'),
            'extraction':({'id':extraction.id, 'mode':extraction.mode, 'records':extraction.result,
                           'reviewed':extraction.reviewed} if extraction else None)})
    return {'id':batch.pk, 'group':batch.group_id, 'created_at':batch.created_at,
            'expires_at':batch.expires_at, 'ai_parameters':batch.parameters, 'skipped':batch.skipped, 'items':items}


def run(group, user, batch, item_id, retry=False):
    if type(item_id) is not int or type(retry) is not bool:
        raise BusinessError('病例任务参数无效。')
    clean_expired(group)
    now = timezone.now()
    try:
        with transaction.atomic():
            # This write acquires SQLite's reservation before inspecting group-wide execution state.
            MedicalRecognitionBatch.objects.filter(pk=batch.pk).update(revision=F('revision')+1)
            item = MedicalRecognitionItem.objects.filter(batch=batch, group=group, pk=item_id).first()
            if not item:
                raise BusinessError('病例任务不存在。', 404)
            if item.status == 'succeeded':
                return item
            if item.status == 'running':
                return item
            if item.status == 'failed' and not retry:
                raise BusinessError('该病例识别失败，请使用重试操作。', 409)
            shared_error = MedicalRecognitionItem.objects.filter(batch=batch, status='failed',
                error_code__in=['auth', 'not_authenticated', 'csrf_failed', 'permission_denied']).exclude(pk=item.pk).exists()
            if shared_error:
                error=BusinessError('共享认证或权限问题尚未解决，请先重试对应病例。',409)
                error.detail['code']='shared_auth';raise error
            if not item.encrypted_text or batch.expires_at <= now:
                raise BusinessError('暂存病历已过期，请重新粘贴并创建识别任务。', 409)
            if MedicalRecognitionItem.objects.filter(group=group, status='running').exists():
                err = BusinessError('本组已有病例正在识别，请等待其完成。', 409)
                err.detail['code'] = 'batch_busy'
                raise err
            lease = secrets.token_hex(16)
            item.status='running'; item.lease=lease; item.attempt+=1; item.started_at=now
            item.finished_at=None; item.error_code=''; item.error_message=''; item.request_id=''
            item.save(update_fields=['status','lease','attempt','started_at','finished_at','error_code','error_message','request_id'])
    except IntegrityError:
        err=BusinessError('本组已有病例正在识别，请等待其完成。',409);err.detail['code']='batch_busy';raise err
    try:
        text = Fernet(master_key()).decrypt(item.encrypted_text.encode('ascii'), ttl=int(TTL.total_seconds())).decode('utf-8')
        extraction = execute(group, user, text, item.plan_token)
        updated = MedicalRecognitionItem.objects.filter(pk=item.pk, status='running', lease=lease).update(
            status='succeeded', extraction=extraction, encrypted_text='', lease='', finished_at=timezone.now(),
            request_id=extraction.ai_parameters.get('request_id') or '')
        if not updated:
            # The extraction is idempotent and remains available for a subsequent retry.
            raise BusinessError('任务状态已变化，请刷新后核对结果。', 409)
    except (InvalidToken, UnicodeError, ValueError):
        MedicalRecognitionItem.objects.filter(pk=item.pk,status='running',lease=lease).update(status='failed',lease='',
            finished_at=timezone.now(),encrypted_text='',error_code='expired',error_message='暂存病历已失效，请重新粘贴。')
    except BusinessError as exc:
        detail = exc.detail if isinstance(exc.detail,dict) else {}
        code = str(detail.get('code','case_validation'))
        message = str(detail.get('message','病例识别失败，请核对后重试。'))[:500]
        MedicalRecognitionItem.objects.filter(pk=item.pk,status='running',lease=lease).update(status='failed',lease='',
            finished_at=timezone.now(),error_code=code,error_message=message,request_id=str(detail.get('request_id',''))[:40])
    except DatabaseError:
        MedicalRecognitionItem.objects.filter(pk=item.pk,status='running',lease=lease).update(status='failed',lease='',
            finished_at=timezone.now(),error_code='database_error',error_message='保存识别结果失败，请核查数据库后重试。')
    except Exception:
        # Do not persist the exception text: it can contain a prompt or patient data.
        MedicalRecognitionItem.objects.filter(pk=item.pk,status='running',lease=lease).update(status='failed',lease='',
            finished_at=timezone.now(),error_code='execution_error',error_message='病例识别执行异常，请检查服务状态后重试。')
    item.refresh_from_db()
    return item
