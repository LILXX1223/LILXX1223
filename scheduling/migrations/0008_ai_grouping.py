from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL), ('scheduling','0007_excel_import')]
    operations = [migrations.CreateModel(name='AIGroupingTask', fields=[
        ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),
        ('effective_date',models.DateField()),
        ('parameters',models.JSONField(default=dict)),
        ('participants',models.JSONField(default=list)),
        ('excluded',models.JSONField(default=list)),
        ('ai_model',models.CharField(max_length=120)),
        ('ai_analysis',models.JSONField(default=dict)),
        ('plans',models.JSONField(default=list)),
        ('reviewed',models.JSONField(default=dict)),
        ('final',models.JSONField(default=dict)),
        ('fingerprint',models.CharField(max_length=64)),
        ('status',models.CharField(default='preview',max_length=16)),
        ('revision',models.PositiveIntegerField(default=1)),
        ('created_at',models.DateTimeField(auto_now_add=True)),
        ('updated_at',models.DateTimeField(auto_now=True)),
        ('confirmed_by',models.ForeignKey(blank=True,null=True,on_delete=django.db.models.deletion.PROTECT,related_name='confirmed_grouping_tasks',to=settings.AUTH_USER_MODEL)),
        ('created_by',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,related_name='created_grouping_tasks',to=settings.AUTH_USER_MODEL)),
        ('group',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,to='scheduling.caregroup')),
    ])]
