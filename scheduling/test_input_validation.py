from datetime import date
from unittest.mock import patch
from io import BytesIO
from openpyxl import Workbook
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient
from scheduling.models import CareGroup, Nurse
from scheduling.services.excel_import import validate_mapping
from scheduling.services.common import BusinessError
from django.core.files.uploadedfile import SimpleUploadedFile


class MalformedInputsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with patch('django.utils.timezone.localdate',return_value=date(2026,10,22)):
            call_command('seed_demo',verbosity=0)
        cls.group=CareGroup.objects.get(name='第3组')
        cls.manager=get_user_model().objects.get(username='group3')

    def test_certificate_name_non_string_is_controlled_error(self):
        nurse=Nurse.objects.first();client=APIClient();client.force_authenticate(self.manager)
        r=client.patch(f'/api/v1/nurses/{nurse.id}/',{'group':self.group.id,'revision':nurse.revision,
            'certifications':[{'name':[],'expires_on':'2030-01-01'}]},format='json')
        self.assertEqual(r.status_code,400)

    def test_excel_mapping_target_non_string_is_controlled_error(self):
        with self.assertRaises(BusinessError) as failure:validate_mapping({'姓名':[]},['姓名'])
        self.assertEqual(failure.exception.status_code,400)

    def test_excel_ignored_field_non_string_is_controlled_error(self):
        workbook=Workbook();workbook.active.append(['姓名']);workbook.active.append(['合成护士'])
        output=BytesIO();workbook.save(output)
        client=APIClient();client.force_authenticate(self.manager)
        upload=SimpleUploadedFile('synthetic.xlsx',output.getvalue())
        created=client.post('/api/v1/imports/excel/',{'group':self.group.id,'file':upload},format='multipart')
        self.assertEqual(created.status_code,200);batch=created.data['data']
        r=client.patch(f"/api/v1/imports/excel/{batch['id']}/",{'group':self.group.id,'revision':batch['revision'],
            'records':[{'row_number':2,'ignored_fields':[[]]}]},format='json')
        self.assertEqual(r.status_code,400)
