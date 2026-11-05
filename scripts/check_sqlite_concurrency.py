"""Run optimistic-lock API tests against an isolated, file-backed SQLite test DB."""
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'icu_scheduler.settings')

import django
django.setup()
from django.core.management import call_command
from django.db import connections


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='icu-file-concurrency-') as directory:
        target = Path(directory) / 'test-concurrency.sqlite3'
        connections['default'].settings_dict['TEST']['NAME'] = str(target)
        call_command('test', 'scheduling.tests.ConcurrentWriteTests', interactive=False, verbosity=2)
