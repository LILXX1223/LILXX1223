from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('scheduling', '0006_nurse_avatar'),
    ]
    operations = [migrations.CreateModel(name='NurseImportBatch', fields=[
        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
        ('filename', models.CharField(max_length=255)),
        ('sheets_data', models.JSONField(default=list)),
        ('selected_sheet', models.PositiveSmallIntegerField(default=0)),
        ('mapping', models.JSONField(default=dict)),
        ('ai_suggestion', models.JSONField(default=dict)),
        ('ai_status', models.CharField(default='not_requested', max_length=20)),
        ('records', models.JSONField(default=list)),
        ('result', models.JSONField(default=list)),
        ('status', models.CharField(default='preview', max_length=16)),
        ('revision', models.PositiveIntegerField(default=1)),
        ('created_at', models.DateTimeField(auto_now_add=True)),
        ('updated_at', models.DateTimeField(auto_now=True)),
        ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
        ('group', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='scheduling.caregroup')),
    ])]
