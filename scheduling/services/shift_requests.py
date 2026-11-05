"""Versioned request changes and their effect on unpublished rosters."""
from datetime import timedelta

from django.db.models import F, Q
from django.utils import timezone

from scheduling.models import Nurse, ScheduleDraft, SchedulePublication, ShiftRequest
from scheduling.services.common import BusinessError, audit, parse_date


def effective_requests(group, start, end):
    # Hiding a historical row is a display operation, not revocation.
    return ShiftRequest.objects.filter(
        group=group, nurse__group=group, decision='approved',
        invalidated_by_deletion=False, superseded_by__isnull=True,
        date__range=(start, end),
    ).order_by('id')


def snapshot(group, start, end):
    """Dependency snapshot, including pending revisions requiring review."""
    return list(ShiftRequest.objects.filter(
        group=group, date__range=(start, end),
        invalidated_by_deletion=False, decision__in=['pending', 'approved'],
        superseded_by__isnull=True,
    ).order_by('id').values('id', 'revision', 'version_number', 'decision', 'date'))


def json_snapshot(group, start, end):
    return [{**item, 'date': str(item['date'])} for item in snapshot(group, start, end)]


def invalidate_for_dates(group, days):
    """Bump optimistic revisions for drafts covering either old or new dates."""
    days = {day for day in days if day}
    for draft in ScheduleDraft.objects.filter(group=group):
        if any(draft.start_date <= day <= draft.start_date + timedelta(days=draft.duration_days - 1) for day in days):
            ScheduleDraft.objects.filter(pk=draft.pk).update(revision=F('revision') + 1,
                request_snapshot={'stale': True}, updated_at=timezone.now())


def impact(item):
    draft_ids = [draft.id for draft in ScheduleDraft.objects.filter(group=item.group)
                 if any(entry.get('request_id') == item.id for entry in draft.entries)]
    publication_ids = [pub.id for pub in SchedulePublication.objects.filter(group=item.group)
                       if any(entry.get('request_id') == item.id for entry in pub.snapshot.get('entries', []))]
    return {'draft_ids': draft_ids, 'publication_ids': publication_ids}


def details(item):
    return {'id': item.id, 'revision': item.revision, 'version_number': item.version_number,
            'revises': item.revises_id, 'nurse': item.nurse_id, 'date': str(item.date),
            'kind': item.kind, 'target_shift': item.target_shift, 'partner': item.partner_id,
            'reason': item.reason, 'decision': item.decision, 'review_note': item.review_note,
            'reviewer': item.reviewer_id, 'reviewed_at': item.reviewed_at.isoformat() if item.reviewed_at else None,
            'superseded_by': item.superseded_by_id}


def validate_fields(group, data):
    from scheduling.services.schedule import effective_shifts
    nurse = Nurse.objects.filter(group=group, pk=data.get('nurse'), status='active').first()
    if not nurse:
        raise BusinessError('申请护士不存在、不在岗或不属于当前大组。', 400)
    day = parse_date(data.get('date'))
    kind = data.get('kind')
    if kind not in ('change', 'day', 'leave', 'swap'):
        raise BusinessError('申请类型不合法。')
    reason = data.get('reason')
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        raise BusinessError('请填写500字以内的申请理由。')
    target = 'DAY' if kind == 'day' else 'OFF' if kind == 'leave' else data.get('target_shift')
    if not isinstance(target,str):
        raise BusinessError('目标班次无效。')
    shift = effective_shifts(group, day).get(target)
    if not shift or not shift.enabled:
        raise BusinessError('目标班次不存在或未启用。')
    partner = None
    if kind == 'swap':
        partner = Nurse.objects.filter(group=group, employee_id=data.get('partner_employee_id'), status='active').first()
        if not partner or partner.pk == nurse.pk:
            raise BusinessError('换班对方不存在、不在岗或与申请人相同。')
    return {'nurse': nurse, 'date': day, 'kind': kind, 'target_shift': target,
            'partner': partner, 'reason': reason.strip()}


def check_conflicts(group, fields, exclude_ids=()):
    participants = [fields['nurse'].pk]
    if fields['partner']:
        participants.append(fields['partner'].pk)
    conflicts = effective_requests(group, fields['date'], fields['date']).exclude(pk__in=exclude_ids).filter(
        Q(nurse_id__in=participants) | Q(kind='swap', partner_id__in=participants))
    if conflicts.exists():
        raise BusinessError('申请涉及的护士当天已有获批申请，请核对双方冲突。', 409)
    pending = ShiftRequest.objects.filter(group=group, date=fields['date'], decision='pending',
        is_deleted=False, invalidated_by_deletion=False).exclude(pk__in=exclude_ids).filter(
        Q(nurse_id__in=participants) | Q(kind='swap', partner_id__in=participants))
    if pending.exists():
        raise BusinessError('申请涉及的护士当天已有待审核申请，请先处理。', 409)


def edit(item, user, data):
    if item.decision not in ('pending', 'approved') or item.superseded_by_id:
        raise BusinessError('该申请已经结束或被替代，不能编辑。', 409)
    fields = validate_fields(item.group, data)
    check_conflicts(item.group, fields, [item.pk, item.revises_id] if item.revises_id else [item.pk])
    before = details(item)
    if item.decision == 'approved':
        if item.request_revisions.filter(decision='pending', is_deleted=False).exists():
            raise BusinessError('该批准申请已有待审核修订，请先处理。', 409)
        version_number = max([item.version_number] + list(item.request_revisions.values_list('version_number', flat=True))) + 1
        # The row id is stable within each version; revises links the full chain.
        revised = ShiftRequest.objects.create(group=item.group, revises=item, version_number=version_number,
            submitted_by=user, **fields)
        after = details(revised)
    else:
        for key, value in fields.items():
            setattr(item, key, value)
        item.nurse_id_at_request = fields['nurse'].id
        item.nurse_name_at_request = fields['nurse'].name
        item.nurse_employee_at_request = fields['nurse'].employee_id
        item.partner_id_at_request = fields['partner'].id if fields['partner'] else None
        item.partner_name_at_request = fields['partner'].name if fields['partner'] else ''
        item.partner_consented = False
        item.partner_confirmed_by = None
        item.partner_confirmation_note = ''
        item.revision += 1
        item.save()
        revised = item
        after = details(item)
    invalidate_for_dates(item.group, [parse_date(before['date']), fields['date']])
    audit(user, item.group, '编辑调班申请', {'before': before, 'after': after,
        'impact': impact(item), 'published_schedule_unchanged': True})
    return revised


def withdraw(item, user, data):
    if item.decision not in ('pending', 'approved') or item.superseded_by_id:
        raise BusinessError('该申请已结束、撤销或被替代，不能重复撤销。', 409)
    if data.get('confirmed') is not True:
        raise BusinessError('请确认撤销该申请。')
    reason = data.get('reason')
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        raise BusinessError('请填写500字以内的撤销原因。')
    before = details(item)
    pending = list(item.request_revisions.filter(decision='pending', is_deleted=False))
    for child in pending:
        child.decision = 'withdrawn'; child.withdrawn_by = user; child.withdrawn_at = timezone.now()
        child.withdrawal_reason = '原批准申请已撤销：' + reason.strip()[:470]
        child.revision += 1; child.save()
        invalidate_for_dates(item.group, [child.date])
        audit(user, item.group, '撤销调班申请修订', {'before_id': child.id, 'after': details(child)})
    affected = impact(item)
    item.decision = 'withdrawn'; item.withdrawn_by = user; item.withdrawn_at = timezone.now()
    item.withdrawal_reason = reason.strip(); item.revision += 1; item.save()
    invalidate_for_dates(item.group, [item.date])
    audit(user, item.group, '撤销调班申请', {'before': before, 'after': details(item),
        'reason': reason.strip(), 'impact': affected, 'published_schedule_unchanged': True})
    return affected
