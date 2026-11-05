from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError


def default_rules():
    return {'anchor': '2026-10-19', 'phases': [0, 1, 2, 3], 'min_staff': {'A': 3, 'DAY': 0, 'N': 3},
            'min_rest_hours': 11, 'max_consecutive_days': 6, 'max_beds': 3, 'normal_beds': 2,
            'max_load': 6, 'high_min_years': 3, 'triple_min_years': 2, 'allow_three_light': True,
            'allow_two_light_medium': True, 'notify_publications': True, 'ward_name': '综合 ICU',
            'ward_code': 'ICU-01', 'clinical_thresholds_confirmed': False}


class CareGroup(models.Model):
    name = models.CharField('大组', max_length=80, unique=True)
    revision = models.PositiveIntegerField(default=1)
    rules = models.JSONField(default=default_rules)
    def __str__(self): return self.name


class Skill(models.Model):
    group = models.ForeignKey(CareGroup, on_delete=models.PROTECT, related_name='skill_catalog')
    code = models.CharField(max_length=48)
    name = models.CharField(max_length=80)
    category = models.CharField(max_length=80, blank=True)
    description = models.CharField(max_length=500, blank=True)
    enabled = models.BooleanField(default=True)
    participates_bed = models.BooleanField(default=True)
    participates_group = models.BooleanField(default=True)
    match_terms = models.JSONField(default=list)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['group', 'code'], name='unique_group_skill_code'),
                       models.UniqueConstraint(fields=['group', 'name'], name='unique_group_skill_name')]

    def __str__(self): return self.name


class Nurse(models.Model):
    group = models.ForeignKey(CareGroup, on_delete=models.PROTECT)
    name = models.CharField('姓名', max_length=50)
    employee_id = models.CharField('工号', max_length=40, unique=True)
    subgroup = models.PositiveSmallIntegerField(default=1, null=True, blank=True)
    is_leader = models.BooleanField('带组老师', default=False)
    level = models.CharField('护理层级', max_length=8, default='N1')
    years = models.PositiveSmallIntegerField('ICU年资', default=0)
    title = models.CharField('职称', max_length=50, default='护师')
    gender = models.CharField(max_length=8, default='女')
    phone = models.CharField(max_length=30, blank=True)
    # An opaque generated filename only; the actual image is served by an authenticated API.
    avatar_key = models.CharField(max_length=48, blank=True, default='', editable=False)
    base_type = models.CharField(max_length=12, choices=[('rotation', '固定轮转'), ('day', '长期固定白班')], default='rotation')
    qualifications = models.JSONField(default=list)
    skills = models.JSONField(default=list)
    # Verified qualifications: [{name, expires_on}], independently approved by group management.
    certifications = models.JSONField(default=list)
    workdays = models.JSONField(default=list)
    can_manage_beds = models.BooleanField(default=True)
    status = models.CharField(max_length=12, choices=[('active','在岗'),('paused','停排'),('left','离职')], default='active')
    effective_from = models.DateField(default='2020-01-01')
    notes = models.CharField(max_length=500, blank=True)
    teaching_collaboration = models.TextField('带教与协作能力', blank=True)
    assessment_level = models.PositiveSmallIntegerField('病情评估能力能级', null=True, blank=True)
    special_restrictions = models.TextField('特殊排班限制', blank=True)
    revision = models.PositiveIntegerField(default=1)
    def __str__(self): return self.name


class NurseSkill(models.Model):
    nurse = models.ForeignKey(Nurse, on_delete=models.CASCADE, related_name='skill_links')
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name='nurse_links')
    level = models.CharField('技能掌握程度', max_length=100, blank=True)
    independent = models.BooleanField('独立护理资格', default=False)
    expires_on = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['nurse', 'skill'], name='unique_nurse_skill')]


class AccountProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='care_profile')
    role = models.CharField(max_length=24, choices=[('disabled_nurse','已停用的旧护士账号'),('group_manager','大组管理'),('head_nurse','护士长'),('system_admin','系统管理员')])
    group = models.ForeignKey(CareGroup, null=True, blank=True, on_delete=models.PROTECT)
    nurse = models.OneToOneField(Nurse, null=True, blank=True, on_delete=models.SET_NULL)

    def clean(self):
        if self.role=='group_manager' and not self.group_id:
            raise ValidationError('大组管理账号必须绑定所属大组。')
        if self.nurse_id and self.nurse.group_id!=self.group_id:
            raise ValidationError('账号大组与护士档案大组必须一致。')


class ShiftType(models.Model):
    group = models.ForeignKey(CareGroup, on_delete=models.PROTECT)
    code = models.CharField(max_length=12)
    name = models.CharField(max_length=30)
    start = models.TimeField(default='08:00')
    end = models.TimeField(default='17:00')
    next_day = models.BooleanField(default=False)
    hours = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    working = models.BooleanField(default=True)
    color = models.CharField(max_length=16, default='#e6f4ff')
    effective_from = models.DateField(default='2020-01-01')
    enabled = models.BooleanField(default=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['group','code','effective_from'], name='unique_shift_effective')]


class ShiftRequest(models.Model):
    group = models.ForeignKey(CareGroup, null=True, on_delete=models.PROTECT)
    nurse = models.ForeignKey(Nurse, null=True, on_delete=models.SET_NULL)
    nurse_id_at_request = models.PositiveBigIntegerField(null=True)
    nurse_name_at_request = models.CharField(max_length=50, blank=True)
    nurse_employee_at_request = models.CharField(max_length=40, blank=True)
    date = models.DateField()
    kind = models.CharField(max_length=16, choices=[('change','调班'),('day','临时白班'),('leave','请假'),('swap','换班')])
    target_shift = models.CharField(max_length=12)
    partner = models.ForeignKey(Nurse, null=True, blank=True, related_name='swap_requests', on_delete=models.SET_NULL)
    partner_id_at_request = models.PositiveBigIntegerField(null=True, blank=True)
    partner_name_at_request = models.CharField(max_length=50, blank=True)
    partner_consented = models.BooleanField(default=False)
    reason = models.CharField(max_length=500)
    decision = models.CharField(max_length=16, default='pending')
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    reviewed_at = models.DateTimeField(null=True)
    review_note = models.CharField(max_length=500, blank=True)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='recorded_shift_requests')
    partner_confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='confirmed_shift_requests')
    partner_confirmation_note = models.CharField(max_length=500, blank=True)
    invalidated_by_deletion = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='hidden_shift_requests')
    delete_reason = models.CharField(max_length=500, blank=True, default='')
    revision = models.PositiveIntegerField(default=1)
    # A revision is a new request row: the previous approval remains intact
    # until the revision is approved, and stays effective if it is rejected.
    revises = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='request_revisions')
    version_number = models.PositiveIntegerField(default=1)
    superseded_by = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    withdrawn_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='withdrawn_shift_requests')
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawal_reason = models.CharField(max_length=500, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.nurse_id:
            if not self.group_id: self.group_id=self.nurse.group_id
            if not self.nurse_id_at_request: self.nurse_id_at_request=self.nurse_id
            if not self.nurse_name_at_request: self.nurse_name_at_request=self.nurse.name
            if not self.nurse_employee_at_request: self.nurse_employee_at_request=self.nurse.employee_id
        if self.partner_id:
            if not self.partner_id_at_request: self.partner_id_at_request=self.partner_id
            if not self.partner_name_at_request: self.partner_name_at_request=self.partner.name
        return super().save(*args, **kwargs)


class ScheduleDraft(models.Model):
    group = models.ForeignKey(CareGroup, on_delete=models.PROTECT)
    start_date = models.DateField()
    duration_days = models.PositiveSmallIntegerField(default=7)
    request_snapshot = models.JSONField(null=True, blank=True, default=None)
    entries = models.JSONField(default=list)
    revision = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['group','start_date'], name='unique_draft_week')]


class ImmutableRecord(models.Model):
    class Meta: abstract = True
    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError('正式版本不可修改，请创建新的草稿。')
        return super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        raise ValidationError('正式版本及审计记录不可删除。')


class ScheduleDraftVersion(ImmutableRecord):
    """Immutable generation snapshot; the existing weekly draft remains editable."""
    draft = models.ForeignKey(ScheduleDraft, on_delete=models.PROTECT, related_name='generation_versions')
    revision = models.PositiveIntegerField()
    entries = models.JSONField(default=list)
    summary = models.JSONField(default=dict)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['draft','revision'], name='unique_schedule_draft_version')]


class NurseDeletionSnapshot(ImmutableRecord):
    group = models.ForeignKey(CareGroup, on_delete=models.PROTECT)
    nurse_id_at_deletion = models.PositiveBigIntegerField()
    snapshot = models.JSONField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class SkillDeletionSnapshot(ImmutableRecord):
    group = models.ForeignKey(CareGroup, on_delete=models.PROTECT)
    skill_id_at_deletion = models.PositiveBigIntegerField()
    code = models.CharField(max_length=48)
    name = models.CharField(max_length=80)
    match_terms = models.JSONField(default=list)
    associations = models.JSONField(default=list)
    participates_bed = models.BooleanField(default=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    recorded_at = models.DateTimeField(auto_now_add=True)


class SchedulePublication(ImmutableRecord):
    group = models.ForeignKey(CareGroup, on_delete=models.PROTECT)
    draft = models.ForeignKey(ScheduleDraft, on_delete=models.PROTECT)
    draft_revision = models.PositiveIntegerField()
    start_date = models.DateField()
    duration_days = models.PositiveSmallIntegerField(default=7)
    snapshot = models.JSONField()
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['draft','draft_revision'], name='unique_schedule_release')]


class Bed(models.Model):
    group = models.ForeignKey(CareGroup, on_delete=models.PROTECT)
    # The primary key remains the stable clinical reference; the physical label
    # may also contain letters (e.g. 101A) and is never changed by range edits.
    number = models.CharField(max_length=32)
    enabled = models.BooleanField(default=True)
    configured = models.BooleanField(default=True)
    visible = models.BooleanField(default=True)
    class Meta:
        ordering = ['number']
        constraints = [models.UniqueConstraint(fields=['group','number'], name='unique_group_bed')]


class BedGroupConfiguration(models.Model):
    group = models.OneToOneField(CareGroup, on_delete=models.PROTECT, related_name='bed_configuration')
    start_number = models.PositiveIntegerField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    updated_at = models.DateTimeField(auto_now=True)


class Patient(models.Model):
    group = models.ForeignKey(CareGroup, on_delete=models.PROTECT)
    bed = models.OneToOneField(Bed, null=True, blank=True, on_delete=models.PROTECT)
    code = models.CharField(max_length=60, unique=True)
    name = models.CharField(max_length=60)
    gender = models.CharField(max_length=8, blank=True)
    age = models.PositiveSmallIntegerField(null=True, blank=True)
    diagnoses = models.JSONField(default=list)
    suspected = models.JSONField(default=list)
    treatments = models.JSONField(default=list)
    historical = models.JSONField(default=list)
    suggestions = models.JSONField(default=list)
    evidence = models.JSONField(default=list)
    load = models.CharField(max_length=10, choices=[('light','较轻'),('medium','稍重'),('high','高负荷')], default='light')
    # Independently reviewed by a manager; never inferred from nursing load or AI text.
    assessment_need = models.CharField('病情评估支持需求', max_length=16, blank=True, default='', choices=[('', '待核对'), ('higher', '需较高评估支持'), ('standard', '未标记较高需求')])
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    reviewed_at = models.DateTimeField(null=True)
    synthetic = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)
    admitted_at = models.DateTimeField(auto_now_add=True)
    discharged_at = models.DateTimeField(null=True)


class Extraction(models.Model):
    group = models.ForeignKey(CareGroup, on_delete=models.PROTECT)
    result = models.JSONField()
    mode = models.CharField(max_length=16)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reviewed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    ai_parameters = models.JSONField(default=dict, blank=True)
    request_token_id = models.CharField(max_length=32, null=True, blank=True, unique=True)


class MedicalRecognitionBatch(models.Model):
    """Owner-scoped batch; results remain ordinary, human-reviewed Extractions."""
    group = models.ForeignKey(CareGroup, on_delete=models.PROTECT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    parameters = models.JSONField(default=dict)
    skipped = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revision = models.PositiveIntegerField(default=1)


class MedicalRecognitionItem(models.Model):
    batch = models.ForeignKey(MedicalRecognitionBatch, on_delete=models.PROTECT, related_name='items')
    group = models.ForeignKey(CareGroup, on_delete=models.PROTECT)
    bed = models.ForeignKey(Bed, on_delete=models.PROTECT)
    bed_number = models.CharField(max_length=32)
    text_digest = models.CharField(max_length=64)
    # Staged source is encrypted and removed on success or expiry; never expose it in admin.
    encrypted_text = models.TextField(blank=True, default='')
    plan_token = models.TextField()
    status = models.CharField(max_length=16, default='waiting')
    attempt = models.PositiveIntegerField(default=0)
    lease = models.CharField(max_length=32, blank=True, default='')
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=48, blank=True, default='')
    error_message = models.CharField(max_length=500, blank=True, default='')
    request_id = models.CharField(max_length=40, blank=True, default='')
    extraction = models.ForeignKey(Extraction, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        ordering = ['id']
        constraints = [models.UniqueConstraint(fields=['batch', 'bed'], name='unique_medical_batch_bed'),
                       models.UniqueConstraint(fields=['group'], condition=models.Q(status='running'),
                                               name='one_medical_running_per_group')]


class PatientRevision(ImmutableRecord):
    """Restricted structured clinical history, kept out of the general operation log."""
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name='versions')
    revision = models.PositiveIntegerField()
    snapshot = models.JSONField()
    action = models.CharField(max_length=40)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['patient', 'revision'], name='unique_patient_revision')]
        ordering = ['-revision']


class BedDraft(models.Model):
    group = models.ForeignKey(CareGroup, on_delete=models.PROTECT)
    date = models.DateField()
    shift = models.CharField(max_length=12)
    roster = models.JSONField(default=list)
    schedule_publication = models.ForeignKey(SchedulePublication, null=True, on_delete=models.PROTECT)
    assignments = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['group','date','shift'], name='unique_bed_draft')]


class BedPublication(ImmutableRecord):
    group = models.ForeignKey(CareGroup, on_delete=models.PROTECT)
    draft = models.ForeignKey(BedDraft, on_delete=models.PROTECT)
    draft_revision = models.PositiveIntegerField()
    date = models.DateField()
    shift = models.CharField(max_length=12)
    snapshot = models.JSONField()
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['draft','draft_revision'], name='unique_bed_release')]


class AuditLog(ImmutableRecord):
    group = models.ForeignKey(CareGroup, null=True, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=80)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class AIServiceConfiguration(models.Model):
    # Single global configuration; API credentials are encrypted, never stored in plaintext.
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    provider = models.CharField(max_length=32, default='deepseek')
    base_url = models.URLField(max_length=500, default='https://api.deepseek.com/v1')
    model = models.CharField(max_length=120, default='deepseek-chat')
    encrypted_key = models.TextField(blank=True, default='')
    revision = models.PositiveIntegerField(default=1)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    updated_at = models.DateTimeField(auto_now=True)


class AISceneConfiguration(models.Model):
    """Per-use settings; credentials remain exclusively in AIServiceConfiguration."""
    scene = models.CharField(max_length=40, unique=True)
    model = models.CharField(max_length=120)
    thinking = models.CharField(max_length=12)
    max_tokens = models.PositiveIntegerField()
    timeout_seconds = models.PositiveIntegerField()
    stream = models.BooleanField(default=False)
    output_format = models.CharField(max_length=16)
    revision = models.PositiveIntegerField(default=1)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    updated_at = models.DateTimeField(auto_now=True)


class AIRequestLog(models.Model):
    """Safe diagnostics only: never retain prompts, responses, credentials or patient text."""
    request_id = models.CharField(max_length=40, unique=True)
    scene = models.CharField(max_length=40)
    parameters = models.JSONField(default=dict)
    status = models.CharField(max_length=16, default='running')
    http_status = models.PositiveSmallIntegerField(null=True)
    error_code = models.CharField(max_length=40, blank=True)
    finish_reason = models.CharField(max_length=40, blank=True)
    elapsed_ms = models.PositiveIntegerField(null=True)
    usage = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    message = models.CharField(max_length=300)
    read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class NurseImportBatch(models.Model):
    """Reviewed Excel staging, never the canonical personnel table."""
    group = models.ForeignKey(CareGroup, on_delete=models.PROTECT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    filename = models.CharField(max_length=255)
    sheets_data = models.JSONField(default=list)
    selected_sheet = models.PositiveSmallIntegerField(default=0)
    mapping = models.JSONField(default=dict)
    ai_suggestion = models.JSONField(default=dict)
    ai_status = models.CharField(max_length=20, default='not_requested')
    records = models.JSONField(default=list)
    result = models.JSONField(default=list)
    status = models.CharField(max_length=16, default='preview')
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class AIGroupingTask(models.Model):
    group = models.ForeignKey(CareGroup, on_delete=models.PROTECT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='created_grouping_tasks')
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='confirmed_grouping_tasks')
    effective_date = models.DateField()
    parameters = models.JSONField(default=dict)
    participants = models.JSONField(default=list)
    excluded = models.JSONField(default=list)
    ai_model = models.CharField(max_length=120)
    ai_analysis = models.JSONField(default=dict)
    plans = models.JSONField(default=list)
    reviewed = models.JSONField(default=dict)
    final = models.JSONField(default=dict)
    fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=16, default='preview')
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class GroupingPublication(ImmutableRecord):
    """Immutable formal group membership snapshot; reversal creates a new version."""
    group = models.ForeignKey(CareGroup, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    task = models.ForeignKey(AIGroupingTask, null=True, blank=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=20)
    reason = models.CharField(max_length=500, blank=True)
    snapshot = models.JSONField(default=dict)
    parameters = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


# Imported here so Django registers the shared allocation task models.
from scheduling.task_models import AllocationExecution, AllocationEvent, AllocationConversation  # noqa: E402,F401
from scheduling.experience_models import AllocationExperience, AllocationFeedback  # noqa: E402,F401
from scheduling.cancellation_models import ScheduleCancellation, ScheduleCancellationEntry, ScheduleCancellationHold  # noqa: E402,F401
