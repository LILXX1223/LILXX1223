import json
import os
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from scheduling.models import AccountProfile, AIServiceConfiguration, AuditLog, CareGroup
from scheduling.services import ai
from scheduling.services.common import BusinessError


class AIConfigurationTests(TestCase):
    def setUp(self):
        self.environment=patch.dict(os.environ,{'ICU_AI_ENCRYPTION_KEY':Fernet.generate_key().decode(),
            'ICU_AI_ALLOW_MOCK':'false','AI_HOSPITAL_AUTHORIZED':'false'})
        self.environment.start();self.addCleanup(self.environment.stop)
        users=get_user_model()
        self.admin=users.objects.create_superuser('admin-ai',password='test-password')
        self.manager=users.objects.create_user('manager-ai',password='test-password')
        self.group=CareGroup.objects.create(name='AI测试组')
        AccountProfile.objects.create(user=self.manager,role='group_manager',group=self.group)
        self.client=APIClient();self.client.force_authenticate(self.admin)
        self.params={'provider':'deepseek','base_url':'https://api.deepseek.com/v1','model':'deepseek-chat'}

    def save_key(self):
        return self.client.patch('/api/v1/ai/config/',{**self.params,'revision':0,'api_key':'sk-synthetic-test-123'},format='json')

    def test_empty_initial_state_and_unauthorized_access(self):
        response=self.client.get('/api/v1/ai/config/')
        self.assertEqual(response.status_code,200)
        self.assertFalse(response.data['data']['configured']);self.assertFalse(response.data['data']['has_key'])
        self.assertEqual(response.data['data']['revision'],0)
        self.assertEqual(ai.ai_status()['mode'],'unconfigured')
        with self.assertRaises(BusinessError) as failure: ai.extract('合成病历：测试数据')
        self.assertEqual(failure.exception.status_code,503)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get('/api/v1/ai/config/').status_code,[401,403])

    def test_configuration_encrypted_masked_and_preserved(self):
        response=self.save_key();self.assertEqual(response.status_code,200)
        obj=AIServiceConfiguration.objects.get(pk=1)
        self.assertNotIn('sk-synthetic-test-123',obj.encrypted_key)
        self.assertNotIn('sk-synthetic-test-123',str(response.data))
        self.assertNotIn('sk-synthetic-test-123',str(AuditLog.objects.latest('id').details))
        self.assertNotIn('sk-synthetic-test-123',str(self.client.get('/api/v1/ai/config/').data))
        self.assertTrue(response.data['data']['configured'])
        updated=self.client.patch('/api/v1/ai/config/',{**self.params,'revision':1,'model':'another-model','api_key':''},format='json')
        self.assertEqual(updated.status_code,200)
        obj.refresh_from_db();self.assertEqual(obj.model,'another-model');self.assertEqual(obj.revision,2)
        self.assertTrue(obj.encrypted_key)
        self.assertEqual(self.client.patch('/api/v1/ai/config/',{**self.params,'revision':1},format='json').status_code,409)

    def test_local_master_key_is_created_outside_database_with_restricted_permissions(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'HOME':directory,'ICU_AI_ENCRYPTION_KEY':''}):
            response=self.save_key()
            self.assertEqual(response.status_code,200)
            keyfile=Path(directory)/'.local/share/icu-scheduler/ai-master.key'
            self.assertTrue(keyfile.is_file());self.assertEqual(keyfile.stat().st_mode & 0o777,0o600)
            self.assertNotIn('sk-synthetic-test-123',keyfile.read_text())
            from scheduling.services.ai_configuration import secret_value
            self.assertEqual(secret_value(),'sk-synthetic-test-123')
            keyfile.unlink()
            with self.assertRaises(BusinessError) as failure: secret_value()
            self.assertEqual(failure.exception.status_code,503)

    def test_only_admin_writes_or_tests_group_manager_reads_status(self):
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.client.get('/api/v1/ai/config/').status_code,200)
        self.assertEqual(self.client.patch('/api/v1/ai/config/',{**self.params,'revision':0,'api_key':'sk-test'},format='json').status_code,403)
        self.assertEqual(self.client.post('/api/v1/ai/test/',self.params,format='json').status_code,403)

    def test_test_connection_requires_key_and_never_fakes_success(self):
        with patch('scheduling.services.ai_configuration.httpx.Client.post') as post:
            response=self.client.post('/api/v1/ai/test/',self.params,format='json')
        self.assertEqual(response.status_code,503);post.assert_not_called()
        self.assertFalse(AIServiceConfiguration.objects.exists())

    @patch('scheduling.services.ai_configuration.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))])
    @patch('scheduling.services.ai_configuration.httpx.Client.post')
    def test_only_real_provider_response_counts_as_success(self,post,dns):
        remote=Mock();remote.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':'OK'}}]};post.return_value=remote
        response=self.client.post('/api/v1/ai/test/',{**self.params,'api_key':'sk-synthetic'},format='json')
        self.assertEqual(response.status_code,200);self.assertTrue(response.data['data']['ok'])
        self.assertFalse(AIServiceConfiguration.objects.exists());self.assertEqual(post.call_count,1)
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'],'Bearer sk-synthetic')
        remote.json.return_value={'choices':[]}
        response=self.client.post('/api/v1/ai/test/',{**self.params,'api_key':'sk-synthetic'},format='json')
        self.assertEqual(response.status_code,502);self.assertNotIn('sk-synthetic',str(response.data))

    @patch('scheduling.services.ai_configuration.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))])
    @patch('scheduling.services.ai_configuration.httpx.Client.post')
    def test_private_network_target_rejected(self,post,dns):
        response=self.client.post('/api/v1/ai/test/',{**self.params,'api_key':'sk-synthetic'},format='json')
        self.assertEqual(response.status_code,400);post.assert_not_called()
        response=self.client.patch('/api/v1/ai/config/',{**self.params,'base_url':'http://127.0.0.1','revision':0},format='json')
        self.assertEqual(response.status_code,400)

    @patch('scheduling.services.ai_configuration.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))])
    @patch('scheduling.services.ai_configuration.httpx.Client.post')
    def test_clinical_extract_uses_shared_service_and_requires_authorization_for_real_data(self,post,dns):
        self.save_key();payload={k:[] for k in ['diagnoses','suspected','treatments','historical','suggestions','evidence']}
        remote=Mock();remote.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(payload)}}]};post.return_value=remote
        result,mode=ai.extract('合成病历：测试数据',synthetic=True)
        self.assertEqual(mode,'external');self.assertIsNone(result['load']);post.assert_called_once()
        with self.assertRaises(BusinessError) as failure: ai.extract('真实患者资料',synthetic=False)
        self.assertEqual(failure.exception.status_code,403);self.assertEqual(post.call_count,1)
