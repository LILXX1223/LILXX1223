"""Audited, human-controlled soft preferences and allocation feedback."""
from django.conf import settings
from django.db import models


class AllocationExperience(models.Model):
    group = models.ForeignKey('scheduling.CareGroup', on_delete=models.PROTECT)
    business_type = models.CharField(max_length=24)
    title = models.CharField(max_length=160)
    description = models.CharField(max_length=1000)
    target = models.CharField(max_length=40)
    weight = models.DecimalField(max_digits=2, decimal_places=1)
    status = models.CharField(max_length=16, default='pending')
    source = models.CharField(max_length=16, default='manual')
    source_model = models.CharField(max_length=120, blank=True)
    source_details = models.JSONField(default=dict)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='created_allocation_experiences')
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='reviewed_allocation_experiences')
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class AllocationFeedback(models.Model):
    group = models.ForeignKey('scheduling.CareGroup', on_delete=models.PROTECT)
    business_type = models.CharField(max_length=24)
    task_reference = models.CharField(max_length=80, blank=True)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    # Minimal, server-captured context for historical group roles or patient tasks.
    context = models.JSONField(default=dict)
    reason = models.CharField(max_length=500, blank=True)
    adopted = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
