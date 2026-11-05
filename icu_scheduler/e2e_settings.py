"""Settings for disposable browser tests. Never points at the live database."""
import os
from pathlib import Path
from .settings import *  # noqa: F401,F403

raw=os.environ.get('ICU_E2E_DATABASE_PATH','')
target=Path(raw).expanduser()
if not raw or not target.is_absolute() or target.resolve()==(BASE_DIR / 'db.sqlite3').resolve():
    raise RuntimeError('浏览器测试必须指定独立的绝对路径数据库，不能使用业务库。')
DATABASES['default']['NAME']=target
NURSE_AVATAR_ROOT=target.parent / 'avatars'
ICU_E2E_MODE=True
