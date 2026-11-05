from unittest.mock import patch
from datetime import date
from django.test import TestCase
from django.core.management import call_command
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient
from scheduling.models import AccountProfile, CareGroup, Nurse, Patient, PatientRevision, BedPublication, AuditLog


class PatientHistoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with patch('django.utils.timezone.localdate',return_value=date(2026,10,22)):
            call_command('seed_demo',verbosity=0)
        cls.manager=get_user_model().objects.get(username='group3')
        cls.group=CareGroup.objects.get(name='第3组')

    def setUp(self):
        self.client=APIClient();self.client.force_authenticate(self.manager)

    def update(self,p,**extra):
        return self.client.post('/api/v1/beds/patient/',{'group':self.group.pk,'bed_id':p.bed_id,
            'patient_id':p.pk,'patient_revision':p.revision,'confirmed':True,
            'fields':{'name':'合成新名称','load':'medium','synthetic':True,'treatments':['气道护理']},**extra},format='json')

    def test_edit_records_before_and_after_without_leaking_to_general_audit(self):
        p=Patient.objects.first();old_name=p.name;revision=p.revision
        self.assertEqual(self.update(p).status_code,200)
        p.refresh_from_db();self.assertEqual(p.revision,revision+1)
        before=p.versions.get(revision=revision);after=p.versions.get(revision=p.revision)
        self.assertEqual(before.snapshot['name'],old_name)
        self.assertEqual(after.snapshot['name'],'合成新名称')
        self.assertEqual(after.snapshot['treatments'],['气道护理'])
        self.assertEqual(after.actor,self.manager)
        self.assertNotIn('合成新名称',str(AuditLog.objects.latest('id').details))
        with self.assertRaises(ValidationError): after.save()

    def test_invalid_edit_leaves_no_partial_history(self):
        p=Patient.objects.first();count=PatientRevision.objects.count()
        self.assertEqual(self.update(p,fields={'name':'合成名称','load':'invalid'}).status_code,400)
        self.assertEqual(PatientRevision.objects.count(),count)

    def test_history_requires_manager_scope(self):
        p=Patient.objects.first();self.update(p)
        url=f'/api/v1/patients/{p.pk}/history/'
        r=self.client.get(url);self.assertEqual(r.status_code,200)
        self.assertEqual(r.data['data'][0]['revision'],p.revision+1)
        other=CareGroup.objects.create(name='另组')
        self.assertEqual(self.client.get(url,{'group':other.pk}).status_code,403)
        user=get_user_model().objects.create_user('legacy-nurse',password='test-password',is_active=False)
        AccountProfile.objects.create(user=user,role='disabled_nurse',group=self.group,nurse=Nurse.objects.first())
        self.client.force_authenticate(user)
        self.assertEqual(self.client.get(url).status_code,403)

    def test_bed_publication_pins_patient_version(self):
        for assignment in BedPublication.objects.get().snapshot['assignments']:
            version=PatientRevision.objects.get(patient_id=assignment['patient'],revision=assignment['patient_revision'])
            self.assertEqual(version.snapshot['code'],assignment['patient_code'])

    def test_discharge_preserves_previous_bed_and_records_new_state(self):
        from scheduling.services.bed_configuration import public_number
        p=Patient.objects.first();number=public_number(p.bed.number)
        r=self.client.post('/api/v1/beds/discharge/',{'group':self.group.pk,'patient_id':p.pk,
            'patient_revision':p.revision,'reason':'合成病例离床测试'},format='json')
        self.assertEqual(r.status_code,200)
        versions=list(p.versions.all())
        self.assertIsNone(versions[0].snapshot['bed_number'])
        self.assertEqual(versions[1].snapshot['bed_number'],number)
