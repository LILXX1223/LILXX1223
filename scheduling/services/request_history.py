"""Hide ended requests without changing their approval or published schedules."""
from django.db import transaction
from django.utils import timezone
from scheduling.models import ShiftRequest
from scheduling.services.common import BusinessError, audit, lock_group


def reason_not_deletable(item):
    if item.is_deleted:return '该申请已经删除。'
    if item.invalidated_by_deletion:return ''
    if item.decision in ('rejected','withdrawn','completed','invalidated'):return ''
    if item.decision=='approved' and item.date<timezone.localdate():return ''
    if item.decision=='pending':return '申请仍在等待审核。'
    return '申请仍可能影响当前或未来排班。'


def inspect(group,items,action):
    if action not in ('delete','restore'):raise BusinessError('操作类型无效。')
    if not isinstance(items,list) or not 1<=len(items)<=100:raise BusinessError('请选择1至100条申请。')
    if any(not isinstance(x,dict) or type(x.get('id')) is not int or type(x.get('revision')) is not int for x in items):
        raise BusinessError('申请编号或版本无效。')
    ids=[x['id'] for x in items]
    if len(ids)!=len(set(ids)):raise BusinessError('申请列表包含重复记录。')
    found={obj.id:obj for obj in ShiftRequest.objects.filter(group=group,id__in=ids)}
    rows=[]
    for selected in items:
        obj=found.get(selected['id']);reason=''
        if not obj:reason='记录不存在或不属于当前管理范围。'
        elif obj.revision!=selected['revision']:reason='记录版本已变化，请刷新。'
        elif action=='delete':reason=reason_not_deletable(obj)
        elif not obj.is_deleted:reason='该申请未处于已删除状态。'
        rows.append({'id':selected['id'],'eligible':not bool(reason),'reason':reason})
    return rows


@transaction.atomic
def apply(group,user,data):
    action=data.get('action');items=data.get('items')
    if data.get('confirmed') is not True:raise BusinessError('请确认历史申请操作。')
    reason=data.get('reason','')
    if not isinstance(reason,str) or len(reason)>500:raise BusinessError('删除原因不能超过500字。')
    lock_group(group);rows=inspect(group,items,action)
    failures=[row for row in rows if not row['eligible']]
    if failures:raise BusinessError('部分记录已变化或不允许此操作，请刷新后核对。',409,failures)
    for row in rows:
        item=ShiftRequest.objects.get(pk=row['id'],group=group)
        item.is_deleted=action=='delete';item.deleted_at=timezone.now() if item.is_deleted else None
        item.deleted_by=user if item.is_deleted else None;item.delete_reason=reason.strip() if item.is_deleted else ''
        item.revision+=1;item.save(update_fields=['is_deleted','deleted_at','deleted_by','delete_reason','revision'])
    audit(user,group,'隐藏历史调班申请' if action=='delete' else '恢复历史调班申请',
        {'request_ids':[row['id'] for row in rows],'reason':reason.strip(),'schedule_unchanged':True})
    return {'count':len(rows),'action':action,'schedule_unchanged':True}
