from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('scheduling', '0002_patientrevision_and_more'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.CreateModel(name='AIServiceConfiguration', fields=[
        ('id', models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False, serialize=False)),
        ('provider', models.CharField(max_length=32, default='deepseek')),
        ('base_url', models.URLField(max_length=500, default='https://api.deepseek.com/v1')),
        ('model', models.CharField(max_length=120, default='deepseek-chat')),
        ('encrypted_key', models.TextField(blank=True, default='')),
        ('revision', models.PositiveIntegerField(default=1)),
        ('updated_at', models.DateTimeField(auto_now=True)),
        ('changed_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
    ])]
