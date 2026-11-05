from datetime import date, timedelta
from django.db.models import F
from rest_framework.exceptions import APIException, PermissionDenied
from scheduling.models import AccountProfile, AuditLog, CareGroup, Notification


class BusinessError(APIException):
    status_code = 400
    def __init__(self, message, status=400, issues=None):
        self.status_code = status
        super().__init__({'message': message, 'issues': issues or []})


def require_revision(obj, value):
    if type(value) is not int or value != obj.revision:
        raise BusinessError('数据已更新，请刷新后重新操作。', 409)


def lock_group(group):
    # Acquire SQLite's write reservation before reads, not SELECT FOR UPDATE (a no-op on SQLite).
    CareGroup.objects.filter(pk=group.pk).update(revision=F('revision') + 1)
    group.refresh_from_db()


def role_of(user):
    if user.is_superuser: return 'system_admin'
    try: return user.care_profile.role
    except AccountProfile.DoesNotExist: return 'unassigned'


def scope(user, group_id=None, write=False):
    if group_id is not None:
        if isinstance(group_id, bool) or not str(group_id).isdigit() or int(group_id) < 1:
            raise BusinessError('大组编号无效。')
    role = role_of(user)
    if write and role not in ['group_manager', 'system_admin']:
        raise PermissionDenied('当前角色没有编辑或发布权限。')
    if role in ['system_admin', 'head_nurse']:
        group = CareGroup.objects.filter(pk=group_id).first() if group_id else CareGroup.objects.first()
    elif role == 'group_manager':
        group = user.care_profile.group
        if not group: raise PermissionDenied('账号尚未绑定大组。')
        if group_id and str(group_id) != str(group.pk): raise PermissionDenied('不能访问其他大组。')
    else: raise PermissionDenied('账号尚未分配业务角色，请联系系统管理员。')
    if not group: raise BusinessError('尚未初始化病区，请运行 seed_demo 管理命令。')
    return group


def audit(user, group, action, details):
    AuditLog.objects.create(actor=user, group=group, action=action, details=details)


def invalidate_drafts(group, schedule=True, bed=True):
    """Dependency edits require a fresh review; immutable releases stay untouched."""
    from django.utils import timezone
    from scheduling.models import ScheduleDraft, BedDraft
    if schedule: ScheduleDraft.objects.filter(group=group).update(revision=F('revision')+1,updated_at=timezone.now())
    if bed: BedDraft.objects.filter(group=group).update(revision=F('revision')+1,updated_at=timezone.now())


def notify(group, message):
    if not group.rules.get('notify_publications', True): return
    for profile in AccountProfile.objects.filter(group=group,user__is_active=True).exclude(role__in=['nurse','disabled_nurse']):
        Notification.objects.create(user=profile.user, message=message)
    for profile in AccountProfile.objects.filter(role='head_nurse').exclude(group=group):
        Notification.objects.create(user=profile.user, message=message)


def parse_date(value):
    try: return date.fromisoformat(str(value))
    except (ValueError, TypeError): raise BusinessError('日期格式须为 YYYY-MM-DD。')


def dates(start, days=7): return [start + timedelta(days=i) for i in range(days)]
