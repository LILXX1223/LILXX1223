from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('scheduling', '0005_nurse_history_and_admin_requests')]
    operations = [migrations.AddField(
        model_name='nurse',
        name='avatar_key',
        field=models.CharField(blank=True, default='', editable=False, max_length=48),
    )]
