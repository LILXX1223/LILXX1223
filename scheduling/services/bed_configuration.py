"""Audited, per-group physical bed configuration; no clinical records are deleted."""
import re
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from scheduling.models import Bed, BedDraft, BedGroupConfiguration, BedPublication, CareGroup, Patient
from scheduling.services.common import BusinessError, audit, invalidate_drafts, lock_group


def normalize_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise BusinessError('床号须为正整数或由字母、数字、连字符组成的自定义编号。')
    number = str(value).strip().upper()
    if not 1 <= len(number) <= 32 or not re.fullmatch(r'[A-Z0-9]+(?:[-_][A-Z0-9]+)*', number):
        raise BusinessError('床号须为正整数或由字母、数字、连字符组成的自定义编号，最多32个字符。')
    if number.isdigit():
        number = str(int(number))
        if not 1 <= int(number) <= 999999:
            raise BusinessError('数字床号须在1至999999之间。')
    return number


def public_number(value):
    """Keep existing numeric API values compatible while supporting text labels."""
    label = str(value)
    return int(label) if label.isdigit() else label


def number_sort_key(value):
    number = str(value)
    return (0, int(number), '') if number.isdigit() else (1, 0, number.upper())


def _config(group):
    return BedGroupConfiguration.objects.filter(group=group).select_related('changed_by').first()


def configuration_data(group):
    config = _config(group)
    all_beds = sorted(Bed.objects.filter(group=group), key=lambda bed: number_sort_key(bed.number))
    occupied = set(Patient.objects.filter(group=group, bed__isnull=False).values_list('bed_id', flat=True))
    active_assignments = _active_bed_ids(group)
    configured = [bed for bed in all_beds if bed.configured]
    numeric = [int(bed.number) for bed in configured if str(bed.number).isdigit()]
    return {
        'start_number': config.start_number if config else (min(numeric) if numeric else None),
        'version': config.version if config else 0,
        'changed_by': config.changed_by.get_username() if config and config.changed_by_id else None,
        'updated_at': config.updated_at.isoformat() if config else None,
        'configured_count': len(configured),
        'visible_count': sum(bed.visible for bed in configured),
        'open_count': sum(bed.enabled for bed in all_beds),
        'beds': [{'id': bed.id, 'number': public_number(bed.number), 'configured': bed.configured,
                  'visible': bed.visible, 'enabled': bed.enabled, 'occupied': bed.id in occupied,
                  'active_assignment': bed.id in active_assignments} for bed in all_beds],
    }


def _active_bed_ids(group):
    """Include current/future drafts and latest releases; keep older releases historical."""
    now = timezone.localtime()
    window = Q(date__gte=now.date())
    if now.hour < 8:
        window |= Q(date=now.date() - timedelta(days=1), shift='N')
    result = set()
    for assignments in BedDraft.objects.filter(window, group=group).values_list('assignments', flat=True):
        for key, nurse in (assignments or {}).items():
            if nurse is not None and str(key).isdigit():
                result.add(int(key))
    latest = {}
    for pub in BedPublication.objects.filter(window, group=group).order_by('id'):
        latest[(pub.date, pub.shift)] = pub
    for pub in latest.values():
        for row in pub.snapshot.get('assignments', []):
            if row.get('nurse') is not None and str(row.get('bed', '')).isdigit():
                result.add(int(row['bed']))
    return result


def _ward(group):
    value = group.rules.get('ward_code')
    if not isinstance(value, str) or not value.strip():
        raise BusinessError('病区编号未设置，无法核查跨组床位冲突。')
    return value.strip().casefold()


def _other_beds(group, ward_code=None):
    ward = ward_code if ward_code is not None else _ward(group)
    groups = [other.pk for other in CareGroup.objects.exclude(pk=group.pk)
              if isinstance(other.rules.get('ward_code'), str)
              and other.rules['ward_code'].strip().casefold() == ward]
    return {str(number).upper() for number in Bed.objects.filter(group_id__in=groups).values_list('number', flat=True)}


def validate_ward_change(group, ward_code):
    ward = ward_code.strip().casefold()
    overlaps = {str(n).upper() for n in Bed.objects.filter(group=group).values_list('number', flat=True)} & _other_beds(group, ward)
    if overlaps:
        raise BusinessError('目标病区中已有其他大组管理相同床号，不能直接修改病区编号；请先核对物理床位归属。', 409)


def _plan(group, data):
    start = data.get('start_number')
    if start is not None and (type(start) is not int or not 1 <= start <= 999999):
        raise BusinessError('起始床位号须为1至999999之间的整数。')
    rows = data.get('beds')
    if not isinstance(rows, list) or len(rows) > 500:
        raise BusinessError('本组床位配置须为不超过500项的列表。')
    if rows and start is None:
        raise BusinessError('请填写用于生成候选床位的起始床号。')
    all_beds = list(Bed.objects.filter(group=group))
    by_id = {b.pk: b for b in all_beds}
    by_number = {str(b.number).upper(): b for b in all_beds}
    seen = set(); chosen = {}; additions = []
    other_numbers = _other_beds(group)
    for row in rows:
        if not isinstance(row, dict) or type(row.get('visible')) is not bool or type(row.get('enabled')) is not bool:
            raise BusinessError('每张床位均须明确显示状态和开放状态。')
        number = normalize_number(row.get('number'))
        if number in seen: raise BusinessError(f'{number}床重复，请核对。')
        seen.add(number)
        bed_id = row.get('id')
        if bed_id is not None and (type(bed_id) is not int or bed_id not in by_id):
            raise BusinessError('床位不存在或不属于当前大组。', 403)
        bed = by_id.get(bed_id) if bed_id is not None else by_number.get(number)
        if bed and str(bed.number).upper() != number:
            raise BusinessError('不能通过修改配置更改现有物理床号；请使用独立的床号更正流程。')
        if number in other_numbers and (not bed or not bed.configured):
            raise BusinessError(f'{number}床已由同病区其他大组管理，不能重复配置。', 409)
        if bed:
            chosen[bed.pk] = {'bed': bed, 'visible': row['visible'], 'enabled': row['enabled']}
        else:
            additions.append({'number': number, 'visible': row['visible'], 'enabled': row['enabled']})
    occupied = set(Patient.objects.filter(group=group, bed__isnull=False).values_list('bed_id', flat=True))
    active = _active_bed_ids(group)
    removed = [bed for bed in all_beds if bed.configured and bed.pk not in chosen]
    changed = [item for item in chosen.values() if not item['bed'].configured or
               item['bed'].visible != item['visible'] or item['bed'].enabled != item['enabled']]
    unsafe = []
    for bed in all_beds:
        item = chosen.get(bed.pk)
        not_managed = item is None or not item['visible'] or not item['enabled']
        if not_managed and bed.pk in occupied | active:
            reason = '在床患者' if bed.pk in occupied else '当前或未来护理责任安排'
            unsafe.append({'number': public_number(bed.number), 'reason': reason})
    return {'start': start, 'chosen': chosen, 'removed': removed, 'changed': changed,
            'additions': additions, 'unsafe': unsafe, 'ward': _ward(group)}


def preview(group, data):
    if type(data.get('revision')) is not int or data['revision'] != group.revision:
        raise BusinessError('病区配置已改变，请刷新。', 409)
    config = _config(group)
    if type(data.get('config_version')) is not int or data['config_version'] != (config.version if config else 0):
        raise BusinessError('床位配置版本已改变，请刷新。', 409)
    plan = _plan(group, data)
    return {'can_save': not plan['unsafe'], 'unsafe': plan['unsafe'],
            'added': [public_number(item['number']) for item in plan['additions']],
            'removed': [public_number(bed.number) for bed in plan['removed']],
            'changed': [public_number(item['bed'].number) for item in plan['changed']],
            'configured_count': len(plan['chosen']) + len(plan['additions'])}


@transaction.atomic
def save(group, user, data):
    if 'beds' not in data:
        raise BusinessError('请使用新版床位设置，逐床确认配置、显示和开放状态。')
    lock_group(group)
    if type(data.get('revision')) is not int or data['revision'] != group.revision - 1:
        raise BusinessError('病区配置已改变，请刷新。', 409)
    config = _config(group)
    if type(data.get('config_version')) is not int or data['config_version'] != (config.version if config else 0):
        raise BusinessError('床位配置版本已改变，请刷新。', 409)
    plan = _plan(group, data)
    if plan['unsafe']:
        raise BusinessError('存在在床患者或当前护理责任的床位不能隐藏、关闭或移出配置，请先处理。', 422,
                            [{'message': f"{item['number']}床：{item['reason']}"} for item in plan['unsafe']])
    before = configuration_data(group)
    for bed in plan['removed']:
        bed.configured = False; bed.visible = False
        bed.save(update_fields=['configured', 'visible'])
    for item in plan['changed']:
        bed = item['bed']; bed.configured = True; bed.visible = item['visible']; bed.enabled = item['enabled']
        bed.save(update_fields=['configured', 'visible', 'enabled'])
    for item in plan['additions']:
        Bed.objects.create(group=group, number=item['number'], configured=True,
                           visible=item['visible'], enabled=item['enabled'])
    if not config: config = BedGroupConfiguration(group=group, version=0)
    config.start_number = plan['start']; config.version += 1; config.changed_by = user
    config.save()
    invalidate_drafts(group, schedule=False, bed=True)
    after = configuration_data(group)
    audit(user, group, '修改床位配置', {'version': config.version, 'before': before, 'after': after})
    return after
