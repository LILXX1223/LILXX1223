"""Private, bounded, re-encoded nurse portrait storage."""
from io import BytesIO
import os
from pathlib import Path
import re
import uuid
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError
from django.conf import settings
from django.db import transaction
from scheduling.api.serializers import NurseSerializer
from scheduling.services.common import BusinessError, audit, lock_group, require_revision

MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_SIDE = 8192
ALLOWED_FORMATS = {'JPEG', 'PNG', 'WEBP'}
SAFE_KEY = re.compile(r'^[0-9a-f]{32}\.webp$')


def storage_root():
    return Path(getattr(settings, 'NURSE_AVATAR_ROOT', settings.BASE_DIR / 'private_uploads' / 'avatars'))


def safe_path(key):
    if not isinstance(key, str) or not SAFE_KEY.fullmatch(key):
        raise BusinessError('头像记录无效，请联系管理员。', 404)
    return storage_root() / key


def normalize(upload):
    if not upload or getattr(upload, 'size', 0) < 1 or upload.size > MAX_BYTES:
        raise BusinessError('请选择不超过5MB的头像图片。')
    filename = str(getattr(upload, 'name', ''))
    if Path(filename).suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp'}:
        raise BusinessError('头像仅支持JPG、JPEG、PNG和WebP。')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(upload) as source:
                if source.format not in ALLOWED_FORMATS: raise BusinessError('图片内容不是支持的格式。')
                width,height = source.size
                if width < 1 or height < 1 or width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
                    raise BusinessError('图片尺寸过大，无法安全处理。')
                source.verify()
        upload.seek(0)
        with Image.open(upload) as source:
            # Re-encode one frame with metadata stripped, discarding EXIF and embedded payloads.
            image = ImageOps.exif_transpose(source)
            image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            image = image.convert('RGBA' if 'A' in image.getbands() else 'RGB')
            output = BytesIO(); image.save(output, format='WEBP', quality=88, method=4)
            return output.getvalue()
    except BusinessError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, ValueError, SyntaxError):
        raise BusinessError('头像图片无法识别或已损坏，请重新选择。')


def discard(key):
    """Remove only a generated portrait; never follow a user-provided filesystem path."""
    if not key or not SAFE_KEY.fullmatch(str(key)): return
    try:
        # Account for an unlikely manual duplicate reference before deleting.
        from scheduling.models import Nurse
        if Nurse.objects.filter(avatar_key=key).exists(): return
        safe_path(key).unlink(missing_ok=True)
    except OSError:
        # The new database value is authoritative; a cleanup failure leaves only an unused file.
        return


def replace(group, nurse, user, revision, upload):
    content = normalize(upload)
    directory = storage_root()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = uuid.uuid4().hex + '.webp'
    path = safe_path(key)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        with transaction.atomic():
            lock_group(group); nurse.refresh_from_db(); require_revision(nurse, revision)
            before = nurse.avatar_key
            nurse.avatar_key = key; nurse.revision += 1
            nurse.save(update_fields=['avatar_key','revision'])
            audit(user,group,'更新护士头像',{'nurse':nurse.id,'revision':nurse.revision,'had_avatar':bool(before)})
            if before: transaction.on_commit(lambda: discard(before))
            result = NurseSerializer(nurse).data
        return result
    except BaseException:
        try: path.unlink(missing_ok=True)
        except OSError: pass
        raise


@transaction.atomic
def remove(group, nurse, user, revision):
    lock_group(group); nurse.refresh_from_db(); require_revision(nurse, revision)
    before=nurse.avatar_key
    if before:
        nurse.avatar_key=''; nurse.revision+=1; nurse.save(update_fields=['avatar_key','revision'])
        audit(user,group,'移除护士头像',{'nurse':nurse.id,'revision':nurse.revision})
        transaction.on_commit(lambda: discard(before))
    return NurseSerializer(nurse).data
