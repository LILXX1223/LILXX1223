from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient
from scheduling.models import AccountProfile, AuditLog, CareGroup, Nurse


def image_file(name='portrait.png', fmt='PNG', size=(32, 24)):
    buffer=BytesIO();Image.new('RGB',size,(21,93,158)).save(buffer,format=fmt)
    return SimpleUploadedFile(name,buffer.getvalue(),content_type='image/'+fmt.lower())


class AvatarTests(TransactionTestCase):
    def setUp(self):
        self.directory=TemporaryDirectory(prefix='icu-avatar-test-')
        self.addCleanup(self.directory.cleanup)
        self.setting=override_settings(NURSE_AVATAR_ROOT=Path(self.directory.name)/'avatars')
        self.setting.enable();self.addCleanup(self.setting.disable)
        self.group=CareGroup.objects.create(name='头像组')
        self.other=CareGroup.objects.create(name='其他组')
        users=get_user_model()
        self.manager=users.objects.create_user('avatar-manager',password='test')
        self.head=users.objects.create_user('avatar-head',password='test')
        AccountProfile.objects.create(user=self.manager,role='group_manager',group=self.group)
        AccountProfile.objects.create(user=self.head,role='head_nurse')
        self.nurse=Nurse.objects.create(group=self.group,name='合成护士',employee_id='AVATAR001')
        self.client=APIClient();self.client.force_authenticate(self.manager)
        self.url=f'/api/v1/nurses/{self.nurse.pk}/avatar/'

    def upload(self,file=None,revision=1,group=None):
        return self.client.post(self.url,{'group':group or self.group.id,'revision':revision,
            'file':file or image_file()},format='multipart')

    def test_upload_serves_private_reencoded_image_and_replace_remove(self):
        response=self.upload();self.assertEqual(response.status_code,200)
        self.nurse.refresh_from_db();self.assertEqual(self.nurse.revision,2)
        self.assertNotIn('avatar_key',response.data['data'])
        self.assertIn('/avatar/',response.data['data']['avatar_url'])
        first=Path(self.directory.name)/'avatars'/self.nurse.avatar_key
        self.assertTrue(first.is_file());self.assertEqual(first.stat().st_mode & 0o777,0o600)
        result=self.client.get(self.url,{'group':self.group.id})
        self.assertEqual(result.status_code,200);self.assertEqual(result['Content-Type'],'image/webp')
        self.assertEqual(result['Cache-Control'],'private, no-store')
        content=b''.join(result.streaming_content)
        with Image.open(BytesIO(content)) as image:self.assertEqual(image.format,'WEBP')
        result=self.upload(image_file('another.jpg','JPEG'),revision=2)
        self.assertEqual(result.status_code,200);self.nurse.refresh_from_db()
        second=Path(self.directory.name)/'avatars'/self.nurse.avatar_key
        self.assertTrue(second.is_file());self.assertFalse(first.exists())
        response=self.client.delete(self.url,{'group':self.group.id,'revision':3},format='json')
        self.assertEqual(response.status_code,200);self.assertIsNone(response.data['data']['avatar_url'])
        self.assertFalse(second.exists());self.assertEqual(self.client.get(self.url).status_code,404)
        self.assertTrue(AuditLog.objects.filter(action='更新护士头像').exists())

    def test_valid_formats_and_uppercase_extensions(self):
        for extension,fmt in [('JPG','JPEG'),('JPEG','JPEG'),('PNG','PNG'),('WEBP','WEBP')]:
            with self.subTest(fmt=fmt,extension=extension):
                self.nurse.refresh_from_db()
                self.assertEqual(self.upload(image_file('test.'+extension,fmt),revision=self.nurse.revision).status_code,200)

    def test_rejects_bad_images_excess_size_and_stale_revision(self):
        invalid=SimpleUploadedFile('wrong.png',b'not an image',content_type='image/png')
        self.assertEqual(self.upload(invalid).status_code,400)
        self.assertEqual(self.upload(image_file('too-wide.png',size=(8193,1))).status_code,400)
        self.assertEqual(self.upload(SimpleUploadedFile('x.png',b'x'*(5*1024*1024+1))).status_code,400)
        self.assertEqual(self.upload(image_file('disguised.png','GIF')).status_code,400)
        self.assertEqual(self.upload(image_file('bad.svg','PNG')).status_code,400)
        self.assertEqual(self.upload(revision=0).status_code,409)
        self.assertFalse(list((Path(self.directory.name)/'avatars').glob('*.webp')))
        self.nurse.refresh_from_db();self.assertEqual(self.nurse.revision,1)

    def test_roles_and_group_scope_are_checked_on_reads_and_writes(self):
        self.assertEqual(self.upload().status_code,200)
        self.assertEqual(self.client.get(self.url,{'group':self.other.pk}).status_code,403)
        self.assertEqual(self.upload(revision=2,group=self.other.pk).status_code,403)
        self.client.force_authenticate(self.head)
        response=self.client.get(self.url,{'group':self.group.pk})
        self.assertEqual(response.status_code,200);response.close()
        self.assertEqual(self.upload(revision=2).status_code,403)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(self.url,{'group':self.group.pk}).status_code,[401,403])

    def test_outer_rollback_does_not_delete_previous_avatar(self):
        from django.db import transaction
        self.assertEqual(self.upload().status_code,200);self.nurse.refresh_from_db()
        before=Path(self.directory.name)/'avatars'/self.nurse.avatar_key
        try:
            with transaction.atomic():
                result=self.client.delete(self.url,{'group':self.group.pk,'revision':2},format='json')
                self.assertEqual(result.status_code,200)
                raise ValueError('rollback')
        except ValueError: pass
        self.nurse.refresh_from_db();self.assertTrue(before.exists());self.assertTrue(self.nurse.avatar_key)
