from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from scheduling.models import AccountProfile, Bed, CareGroup, Patient


class BatchAuthenticationTests(TestCase):
    def setUp(self):
        self.group=CareGroup.objects.create(name='合成认证测试组')
        self.other=CareGroup.objects.create(name='合成其他组')
        self.user=get_user_model().objects.create_user('batch-user',password='test-password')
        AccountProfile.objects.create(user=self.user,role='group_manager',group=self.group)
        self.beds=[Bed.objects.create(group=self.group,number=i) for i in range(1,54)]
        for bed in self.beds[:2]:Patient.objects.create(group=self.group,bed=bed,name='合成患者',code='SYN-AUTH-'+str(bed.number),synthetic=True)
        self.client=APIClient();self.client.force_authenticate(self.user)

    def test_53_beds_only_two_occupied_are_prepared(self):
        text='\n'.join(f'{i}床：姓名：合成病例{i}；诊断：合成感染。' for i in range(1,54))
        with patch('scheduling.api.beds.ai.extract') as model:
            response=self.client.post('/api/v1/beds/extract-prepare/',{'group':self.group.id,'mode':'batch','text':text,'synthetic':True},format='json')
        self.assertEqual(response.status_code,200);result=response.data['data']
        self.assertEqual([item['bed_number'] for item in result['records']],[1,2])
        self.assertEqual(len(result['skipped']),51);model.assert_not_called()
        self.assertEqual(Patient.objects.filter(group=self.group).count(),2)

    def test_explicit_synthetic_empty_bed_and_occupancy_conflict(self):
        data={'group':self.group.id,'mode':'batch','text':'3床：姓名：合成病例；诊断：感染。','synthetic':True}
        skipped=self.client.post('/api/v1/beds/extract-prepare/',data,format='json').data['data']
        self.assertEqual(len(skipped['records']),0);self.assertEqual(len(skipped['skipped']),1)
        prepared=self.client.post('/api/v1/beds/extract-prepare/',{**data,'include_empty_beds':True},format='json').data['data']['records'][0]
        Patient.objects.create(group=self.group,bed=self.beds[2],name='合成新入住',code='SYN-NEW',synthetic=True)
        with patch('scheduling.api.beds.ai.extract') as model:
            result=self.client.post('/api/v1/beds/extract-prepared/',{'group':self.group.id,**prepared},format='json')
        self.assertEqual(result.status_code,409);model.assert_not_called()

    def test_real_existing_patient_cannot_be_sent_as_synthetic(self):
        patient=Patient.objects.get(bed=self.beds[0]);patient.synthetic=False;patient.save(update_fields=['synthetic'])
        payload={'group':self.group.id,'mode':'single','bed_number':1,'text':'合成文本用于保护测试。','synthetic':True}
        with patch('scheduling.api.beds.ai.extract') as model:
            response=self.client.post('/api/v1/beds/extract-prepare/',payload,format='json')
            legacy=self.client.post('/api/v1/beds/extract/',payload,format='json')
        self.assertEqual(response.status_code,403);self.assertEqual(legacy.status_code,403);model.assert_not_called()

    def test_missing_login_scope_and_csrf_are_distinct(self):
        self.client.force_authenticate(None)
        data={'group':self.group.id,'mode':'single','bed_number':1,'text':'合成病例测试。','synthetic':True}
        unauth=self.client.post('/api/v1/beds/extract-prepare/',data,format='json')
        self.assertEqual(unauth.status_code,403);self.assertEqual(unauth.data['error']['code'],'not_authenticated')
        self.assertEqual(unauth.data['error']['stage'],'preflight');self.assertTrue(unauth.data['error']['request_id'])
        self.assertNotIn('ErrorDetail',str(unauth.data))
        self.client.force_authenticate(self.user)
        denied=self.client.post('/api/v1/beds/extract-prepare/',{**data,'group':self.other.id},format='json')
        self.assertEqual(denied.status_code,403);self.assertEqual(denied.data['error']['code'],'permission_denied')
        csrf_client=APIClient(enforce_csrf_checks=True);self.assertTrue(csrf_client.login(username='batch-user',password='test-password'))
        csrf=csrf_client.post('/api/v1/beds/extract-prepare/',data,format='json')
        self.assertEqual(csrf.status_code,403);self.assertEqual(csrf.data['error']['code'],'csrf_failed')

    def test_unclear_boundary_is_reported_as_preflight_error(self):
        response=self.client.post('/api/v1/beds/extract-prepare/',{'group':self.group.id,'mode':'batch',
            'text':'没有明确床号的合成病历内容。','synthetic':True},format='json')
        self.assertEqual(response.status_code,400)
        self.assertEqual(response.data['error']['code'],'case_boundary')
        self.assertEqual(response.data['error']['stage'],'preflight')
        self.assertFalse(response.data['error']['retryable'])
