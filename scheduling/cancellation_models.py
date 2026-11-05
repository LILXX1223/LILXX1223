"""Auditable schedule cancellations without modifying published snapshots."""
from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError


class ScheduleCancellation(models.Model):
    group=models.ForeignKey('scheduling.CareGroup',on_delete=models.PROTECT)
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    criteria=models.JSONField(default=dict)
    snapshot=models.JSONField(default=dict)
    reason=models.CharField(max_length=500)
    created_at=models.DateTimeField(auto_now_add=True)

    def save(self,*args,**kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists(): raise ValidationError('撤销记录不可修改。')
        super().save(*args,**kwargs)

    def delete(self,*args,**kwargs): raise ValidationError('撤销记录不可删除。')


class ScheduleCancellationEntry(models.Model):
    cancellation=models.ForeignKey(ScheduleCancellation,on_delete=models.PROTECT,related_name='entries')
    group=models.ForeignKey('scheduling.CareGroup',on_delete=models.PROTECT)
    publication=models.ForeignKey('scheduling.SchedulePublication',null=True,blank=True,on_delete=models.PROTECT)
    draft=models.ForeignKey('scheduling.ScheduleDraft',null=True,blank=True,on_delete=models.PROTECT)
    nurse_id_at_action=models.PositiveBigIntegerField()
    date=models.DateField()
    shift=models.CharField(max_length=24)

    class Meta:
        indexes=[models.Index(fields=['group','date','publication'],name='sched_cancel_group_day_pub')]


class ScheduleCancellationHold(models.Model):
    """Preserve cancellation markers; explicit new draft generations are not masked."""
    group=models.ForeignKey('scheduling.CareGroup',on_delete=models.PROTECT)
    nurse_id_at_action=models.PositiveBigIntegerField()
    date=models.DateField()
    cancellation=models.ForeignKey(ScheduleCancellation,on_delete=models.PROTECT)
    active=models.BooleanField(default=True)
    resolved_at=models.DateTimeField(null=True,blank=True)

    class Meta:
        constraints=[models.UniqueConstraint(fields=['group','nurse_id_at_action','date'],name='one_schedule_cancel_hold')]
