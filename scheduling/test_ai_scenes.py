import json
from unittest.mock import patch

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from scheduling.models import AccountProfile, AIRequestLog, AISceneConfiguration, Bed, CareGroup, Extraction, Patient, Nurse
from scheduling.services.ai_configuration import chat_completion
from scheduling.services.ai_scenes import scene_snapshot, request_options
from scheduling.services import ai
from scheduling.services.common import BusinessError


class SceneTests(TestCase):
    def setUp(self):
        self.admin=get_user_model().objects.create_superuser('scene_admin',password='test-password')
        self.manager=get_user_model().objects.create_user('scene_manager',password='test-password')
        self.group=CareGroup.objects.create(name='测试大组')
        AccountProfile.objects.create(user=self.manager,role='group_manager',group=self.group)
        self.client=APIClient();self.client.force_authenticate(self.admin)
        self.settings={'provider':'deepseek','base_url':'https://api.deepseek.com','model':'deepseek-flash','revision':7}

    def test_independent_settings_authorization_and_revision(self):
        defaults=self.client.get('/api/v1/ai/scenarios/').data['data']
        one=next(x for x in defaults if x['scene']=='medical_single')
        group=next(x for x in defaults if x['scene']=='group_assignment')
        self.assertEqual((one['thinking'],one['max_tokens'],one['timeout_seconds']),('disabled',8192,120))
        self.assertEqual((group['thinking'],group['max_tokens'],group['timeout_seconds']),('high',16384,180))
        update={**one,'model':'deepseek-flash','thinking':'max','max_tokens':10000,'timeout_seconds':150}
        response=self.client.patch('/api/v1/ai/scenarios/',update,format='json')
        self.assertEqual(response.status_code,200);self.assertEqual(response.data['data']['revision'],one['revision']+1)
        self.assertEqual(self.client.patch('/api/v1/ai/scenarios/',update,format='json').status_code,409)
        self.assertEqual(scene_snapshot('medical_single')['thinking'],'max')
        self.assertEqual(scene_snapshot('medical_batch')['thinking'],'disabled')
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.client.patch('/api/v1/ai/scenarios/',{**update,'revision':one['revision']+1},format='json').status_code,403)
        self.assertEqual(self.client.post('/api/v1/ai/scenarios/test/',{'scene':'medical_single'},format='json').status_code,403)

    def test_thinking_parameters_and_immutable_snapshot(self):
        old=scene_snapshot('medical_single',self.settings)
        for mode in ['disabled','low','high','max']:
            snapshot={**old,'thinking':mode}
            opts=request_options(snapshot)
            self.assertEqual(opts['thinking']['type'],'disabled' if mode=='disabled' else 'enabled')
            if mode=='disabled':self.assertNotIn('reasoning_effort',opts)
            else:self.assertEqual(opts['reasoning_effort'],mode)
        AISceneConfiguration.objects.filter(scene='medical_single').update(model='deepseek-flash',thinking='high',
            max_tokens=4096,timeout_seconds=90,stream=False,output_format='json_object')
        self.assertEqual(old['thinking'],'disabled')
        self.assertEqual(scene_snapshot('medical_single',self.settings)['thinking'],'high')

    def test_started_allocation_keeps_its_original_scene_snapshot(self):
        from scheduling.services.allocation_execution import start
        from django.utils import timezone
        Nurse.objects.create(group=self.group,name='合成护士',employee_id='SYN-SCENE',base_type='rotation',status='active')
        task=start(self.group,self.manager,'GROUP_ASSIGNMENT',{'consent':True,'synthetic':True,
            'group_count':1,'group_sizes':[1],'min_leaders':0,'effective_date':str(timezone.localdate())},dispatch=False)
        self.assertEqual(task.parameters['ai_config']['thinking'],'high')
        AISceneConfiguration.objects.filter(scene='group_assignment').update(model='different-model',thinking='low',
            max_tokens=4096,timeout_seconds=90,stream=False,output_format='json_object')
        task.refresh_from_db()
        self.assertEqual(task.parameters['ai_config']['thinking'],'high')
        self.assertNotEqual(task.parameters['ai_config']['model'],'different-model')
        self.assertEqual(scene_snapshot('group_assignment')['thinking'],'low')

    def test_http_errors_json_and_diagnostics_do_not_store_content(self):
        snapshot=scene_snapshot('medical_single',self.settings)
        request=httpx.Request('POST','https://example.com')
        scenarios=[(httpx.Response(401,request=request), 'auth'),(httpx.Response(429,request=request),'rate_limit'),
            (httpx.Response(400,request=request),'invalid_request'),
            (httpx.Response(200,request=request,json={'choices':[{'finish_reason':'length','message':{'content':'{}'}}]}),'truncated'),
            (httpx.Response(200,request=request,json={'choices':[{'finish_reason':'stop','message':{'content':''}}]}),'empty'),
            (httpx.Response(200,request=request,json={'choices':[{'finish_reason':'stop','message':{'content':'{bad'}}]}),'invalid_json')]
        with patch('scheduling.services.ai_configuration.check_external_host',return_value='8.8.8.8'), \
             patch('scheduling.services.ai_configuration.httpx.Client.post') as post:
            for response,code in scenarios:
                with self.subTest(code=code):
                    post.return_value=response
                    with self.assertRaises(BusinessError) as err:
                        chat_completion(self.settings,'synthetic-secret',[{'role':'user','content':'synthetic-patient-content'}],scene_config=snapshot)
                    self.assertEqual(str(err.exception.detail['code']),code)
                    log=AIRequestLog.objects.latest('id');self.assertEqual(log.error_code,code)
                    self.assertNotIn('synthetic-secret',str(log.parameters));self.assertNotIn('synthetic-patient-content',str(log.parameters))
            post.side_effect=httpx.ReadTimeout('timeout')
            with self.assertRaises(BusinessError) as err:chat_completion(self.settings,'synthetic-secret',[],scene_config=snapshot)
            self.assertEqual(str(err.exception.detail['code']),'timeout')

    def test_batch_subtasks_retry_and_confirm_once(self):
        Bed.objects.create(group=self.group,number=1);Bed.objects.create(group=self.group,number=2)
        self.client.force_authenticate(self.manager)
        long_text='合成病程记录，当前患者状态稳定。'*290
        text=f'1床：住院号：SYN-1；姓名：合成甲；{long_text}\n1床：住院号：SYN-1；再次记录：状态稳定。\n2床：住院号：SYN-2；姓名：合成乙；{long_text}'
        self.assertGreater(len(text),9000)
        prepared=self.client.post('/api/v1/beds/extract-prepare/',{'group':self.group.id,'mode':'batch','text':text,'synthetic':True,'include_empty_beds':True},format='json')
        self.assertEqual(prepared.status_code,200);items=prepared.data['data']['records'];self.assertEqual(len(items),2)
        self.assertIn('再次记录',items[0]['text'])
        fields={'name':'合成患者','gender':'','age':None,'diagnoses':[],'suspected':[],'treatments':[],
            'historical':[],'suggestions':[],'evidence':[],'load':'light','synthetic':True}
        with patch('scheduling.api.beds.ai.extract',return_value=(fields,'external',{'request_id':'synthetic-test'})) as model:
            first=self.client.post('/api/v1/beds/extract-prepared/',{'group':self.group.id,**items[0]},format='json')
            self.assertEqual(first.status_code,200)
            repeated=self.client.post('/api/v1/beds/extract-prepared/',{'group':self.group.id,**items[0]},format='json')
            self.assertEqual(repeated.data['data']['id'],first.data['data']['id']);self.assertEqual(model.call_count,1)
            model.side_effect=BusinessError('模拟超时',502)
            failed=self.client.post('/api/v1/beds/extract-prepared/',{'group':self.group.id,**items[1]},format='json')
            self.assertEqual(failed.status_code,502);self.assertEqual(Extraction.objects.count(),1)
            model.side_effect=None
            second=self.client.post('/api/v1/beds/extract-prepared/',{'group':self.group.id,**items[1]},format='json')
        self.assertEqual(Patient.objects.count(),0)
        entries=[{'extraction':r.data['data']['id'],'records':r.data['data']['records']} for r in [first,second]]
        result=self.client.post('/api/v1/beds/review-batch/',{'group':self.group.id,'items':entries,'confirmed':True},format='json')
        self.assertEqual(result.status_code,200);self.assertEqual(Patient.objects.count(),2)
        self.assertEqual(self.client.post('/api/v1/beds/review-batch/',{'group':self.group.id,'items':entries,'confirmed':True},format='json').status_code,409)
        self.assertEqual(Extraction.objects.count(),2)
        with self.assertRaises(BusinessError):ai.split_records('1床：患者甲\n1床：患者乙','batch',None)
