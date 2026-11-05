"""Persistent progress records for allocation tasks; never store patient narratives."""
from django.conf import settings
from django.db import models
from django.db.models import Q


class AllocationExecution(models.Model):
    group = models.ForeignKey('scheduling.CareGroup', on_delete=models.PROTECT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    kind = models.CharField(max_length=24, choices=[('GROUP_ASSIGNMENT', '护士分组'), ('BED_ASSIGNMENT', '床位分配')])
    status = models.CharField(max_length=16, default='queued')
    parameters = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    error = models.CharField(max_length=500, blank=True)
    current_stage = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['group', 'kind'],
            condition=Q(status__in=['queued', 'running']), name='one_active_allocation_per_kind')]


class AllocationEvent(models.Model):
    task = models.ForeignKey(AllocationExecution, on_delete=models.PROTECT, related_name='events')
    stage = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=16)
    summary = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']


class AllocationConversation(models.Model):
    group = models.ForeignKey('scheduling.CareGroup', on_delete=models.PROTECT)
    kind = models.CharField(max_length=24)
    messages = models.JSONField(default=list)
    revision = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['group', 'kind'], name='unique_allocation_conversation')]
