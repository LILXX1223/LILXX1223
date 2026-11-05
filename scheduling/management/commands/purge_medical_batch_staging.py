from django.core.management.base import BaseCommand
from scheduling.models import CareGroup
from scheduling.services.medical_batches import clean_expired


class Command(BaseCommand):
    help = '清除已过期的批量病历加密暂存并释放超时任务；不删除已识别结果。'

    def handle(self, *args, **options):
        for group in CareGroup.objects.all().iterator():
            clean_expired(group)
        self.stdout.write('已检查过期暂存。')
