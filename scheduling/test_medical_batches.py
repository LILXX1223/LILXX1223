from datetime import timedelta
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from scheduling.models import (AccountProfile, AISceneConfiguration, Bed, CareGroup, Extraction,
    MedicalRecognitionBatch, MedicalRecognitionItem, Patient)
from scheduling.services.common import BusinessError


class MedicalBatchTests(TestCase):
    def setUp(self):
        self.key_patch=patch.dict('os.environ', {'ICU_AI_ENCRYPTION_KEY':Fernet.generate_key().decode()})
        self.key_patch.start();self.addCleanup(self.key_patch.stop)
        self.group=CareGroup.objects.create(name='批次测试组')
        self.other=CareGroup.objects.create(name='其他测试组')
        self.user=get_user_model().objects.create_user('batch_manager',password='password')
        AccountProfile.objects.create(user=self.user,role='group_manager',group=self.group)
        self.client=APIClient();self.client.force_authenticate(self.user)
        self.beds=[Bed.objects.create(group=self.group,number=str(n)) for n in range(36,41)]
        AISceneConfiguration.objects.filter(scene='medical_single').update(model='single-model',thinking='high',timeout_seconds=120)
        AISceneConfiguration.objects.filter(scene='medical_batch').update(model='batch-model',thinking='max',timeout_seconds=10)
        self.fields={'name':'合成患者','gender':'','age':None,'diagnoses':[],'suspected':[],
            'treatments':[],'historical':[],'suggestions':[],'evidence':[],'load':'light','synthetic':True}

    def create(self, count=5, **kwargs):
        data={'group':self.group.pk,'mode':'batch','synthetic':True,'include_empty_beds':True,
              'text':'\n'.join(f'{n}床：姓名：合成{n}；诊断：肺炎；当前机械通气。' for n in range(36,36+count)),**kwargs}
        response=self.client.post('/api/v1/medical-batches/',data,format='json')
        self.assertEqual(response.status_code,200,response.data)
        return response.data['data']['batch']

    def run_item(self, batch, item, retry=False):
        return self.client.post(f'/api/v1/medical-batches/{batch}/run/',
            {'group':self.group.pk,'item':item,'retry':retry},format='json')

    def test_five_cases_persist_independently_and_failed_case_retries_once(self):
        batch=self.create();items=batch['items']
        self.assertEqual(len(items),5)
        self.assertEqual(batch['ai_parameters']['configuration_source'],'medical_single')
        self.assertEqual((batch['ai_parameters']['model'],batch['ai_parameters']['thinking'],batch['ai_parameters']['timeout_seconds']),('single-model','high',120))
        stored=MedicalRecognitionItem.objects.get(pk=items[0]['id'])
        self.assertNotIn('合成36',stored.encrypted_text)
        failure=BusinessError('模型请求超时',502);failure.detail['code']='timeout';failure.detail['request_id']='test-timeout'
        with patch('scheduling.services.ai.extract',side_effect=[(self.fields,'external',{'request_id':'first'}),failure,
                (self.fields,'external',{}),(self.fields,'external',{}),(self.fields,'external',{}),(self.fields,'external',{})]) as model:
            for item in items:
                response=self.run_item(batch['id'],item['id'])
                self.assertEqual(response.status_code,200,response.data)
            self.assertEqual(model.call_count,5)
            restored=self.client.get(f"/api/v1/medical-batches/{batch['id']}/?group={self.group.pk}").data['data']
            self.assertEqual([i['status'] for i in restored['items']],['succeeded','failed','succeeded','succeeded','succeeded'])
            self.assertEqual(restored['items'][1]['error_code'],'timeout')
            self.assertEqual(Extraction.objects.count(),4);self.assertEqual(Patient.objects.count(),0)
            self.assertEqual(self.run_item(batch['id'],items[0]['id']).status_code,200)
            self.assertEqual(model.call_count,5)
            self.assertEqual(self.run_item(batch['id'],items[1]['id'],True).status_code,200)
            self.assertEqual(model.call_count,6)
        self.assertEqual(Extraction.objects.count(),5)
        self.assertFalse(MedicalRecognitionItem.objects.filter(batch_id=batch['id']).exclude(encrypted_text='').exists())
        self.assertEqual(MedicalRecognitionItem.objects.get(pk=items[1]['id']).attempt,2)
        # Results are not imported until a separate, explicit human review.
        first=restored['items'][0]['extraction']
        review=self.client.post('/api/v1/beds/review-batch/',{'group':self.group.pk,'confirmed':True,
            'items':[{'extraction':first['id'],'records':first['records']}]},format='json')
        self.assertEqual(review.status_code,200,review.data)
        self.assertEqual(Patient.objects.count(),1)
        self.assertEqual(self.client.post('/api/v1/beds/review-batch/',{'group':self.group.pk,'confirmed':True,
            'items':[{'extraction':first['id'],'records':first['records']}]},format='json').status_code,409)

    def test_saved_batch_scene_and_owner_scope(self):
        batch=self.create(2,parameter_source='medical_batch')
        self.assertEqual(batch['ai_parameters']['model'],'batch-model')
        self.assertEqual(batch['ai_parameters']['timeout_seconds'],10)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(f"/api/v1/medical-batches/{batch['id']}/?group={self.group.pk}").status_code,403)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(f"/api/v1/medical-batches/{batch['id']}/?group={self.other.pk}").status_code,403)
        another=get_user_model().objects.create_user('other_manager')
        AccountProfile.objects.create(user=another,role='group_manager',group=self.group)
        self.client.force_authenticate(another)
        self.assertEqual(self.client.get(f"/api/v1/medical-batches/{batch['id']}/?group={self.group.pk}").status_code,404)

    def test_empty_beds_skipped_and_case_boundaries_checked(self):
        Patient.objects.create(group=self.group,bed=self.beds[0],name='合成患者',code='BATCH-36',synthetic=True)
        batch=self.create(2,include_empty_beds=False)
        self.assertEqual(len(batch['items']),1);self.assertEqual(len(batch['skipped']),1)
        response=self.client.post('/api/v1/medical-batches/',{'group':self.group.pk,'mode':'batch','synthetic':True,
            'include_empty_beds':True,'text':'36床：病例甲\n36床：病例乙'},format='json')
        self.assertEqual(response.status_code,400)
        self.assertEqual(response.data['error']['code'],'case_boundary')
        legacy=self.client.post('/api/v1/beds/extract/',{'group':self.group.pk,'mode':'batch','synthetic':True,
            'include_empty_beds':True,'text':'36床：合成病例\n37床：合成病例'},format='json')
        self.assertEqual(legacy.status_code,409)

    def test_one_running_per_group_and_expiry(self):
        batch=self.create(2);items=batch['items']
        MedicalRecognitionItem.objects.filter(pk=items[0]['id']).update(status='running',started_at=timezone.now(),lease='running')
        blocked=self.run_item(batch['id'],items[1]['id'])
        self.assertEqual(blocked.status_code,409);self.assertEqual(blocked.data['error']['code'],'batch_busy')
        MedicalRecognitionItem.objects.filter(pk=items[0]['id']).update(status='waiting',lease='')
        MedicalRecognitionBatch.objects.filter(pk=batch['id']).update(expires_at=timezone.now()-timedelta(seconds=1))
        restored=self.client.get(f"/api/v1/medical-batches/{batch['id']}/?group={self.group.pk}").data['data']
        self.assertEqual([i['error_code'] for i in restored['items']],['expired','expired'])
        self.assertFalse(MedicalRecognitionItem.objects.filter(batch_id=batch['id']).exclude(encrypted_text='').exists())

    def test_patient_change_during_model_call_does_not_save_extraction(self):
        batch=self.create(1)
        def change_patient(*args,**kwargs):
            Patient.objects.create(group=self.group,bed=self.beds[0],name='新合成患者',code='NEW-36',synthetic=True)
            return self.fields,'external',{}
        with patch('scheduling.services.ai.extract',side_effect=change_patient):
            response=self.run_item(batch['id'],batch['items'][0]['id'])
        self.assertEqual(response.data['data']['items'][0]['status'],'failed')
        self.assertEqual(Extraction.objects.count(),0)

    def test_long_text_multiple_course_records_and_noncontinuous_beds(self):
        Bed.objects.create(group=self.group,number='101A')
        filler='合成病程记录，患者当前状态平稳。'*650
        text=f'36床：住院号：SYN-36；姓名：合成甲；{filler}\n36床：住院号：SYN-36；复查：目前稳定。\n101A床：姓名：合成乙；诊断：感染。'
        self.assertGreater(len(text),9219)
        response=self.client.post('/api/v1/medical-batches/',{'group':self.group.pk,'mode':'batch',
            'synthetic':True,'include_empty_beds':True,'text':text},format='json')
        self.assertEqual(response.status_code,200,response.data)
        batch=response.data['data']['batch']
        self.assertEqual([i['bed_number'] for i in batch['items']],['36','101A'])
        self.assertEqual(MedicalRecognitionItem.objects.filter(batch_id=batch['id']).count(),2)
