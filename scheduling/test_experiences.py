import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from scheduling.experience_models import AllocationExperience
from scheduling.models import AccountProfile, Bed, CareGroup, Nurse
from scheduling.services import experiences
from scheduling.services.common import BusinessError


class ExperienceTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('experience-manager')
        self.group=CareGroup.objects.create(name='经验测试组')
        self.group_other=CareGroup.objects.create(name='其他组')
        AccountProfile.objects.create(user=self.user,role='group_manager',group=self.group)
        self.nurse=Nurse.objects.create(group=self.group,name='合成护士',employee_id='SYN-E1')
        self.bed=Bed.objects.create(group=self.group,number=1)
        self.group_data={'business_type':experiences.GROUP,'title':'平衡技能','description':'兼顾技能覆盖',
                         'target':'skill_coverage','weight':0.8}
        self.bed_data={'business_type':experiences.BED,'title':'平衡负荷','description':'尽量分散护理负荷',
                       'target':'load_balance','weight':0.7}

    def test_experience_is_pending_until_explicitly_approved_and_pause_is_effective(self):
        item=experiences.create(self.group,self.user,self.group_data)
        defaults={'skill_coverage':0.2}
        self.assertEqual(experiences.resolve_weights(self.group,experiences.GROUP,defaults)['weights'],defaults)
        with self.assertRaises(BusinessError):experiences.change_status(self.group,self.user,item,{'revision':1,'action':'adopt'})
        item=experiences.change_status(self.group,self.user,item,{'revision':1,'action':'adopt','confirmed':True})
        result=experiences.resolve_weights(self.group,experiences.GROUP,defaults)
        self.assertEqual(result['weights']['skill_coverage'],0.8);self.assertEqual(result['applied'][0]['id'],item.id)
        with self.assertRaises(BusinessError):experiences.change_status(self.group,self.user,item,{'revision':1,'action':'pause','confirmed':True})
        experiences.change_status(self.group,self.user,item,{'revision':2,'action':'pause','confirmed':True})
        self.assertEqual(experiences.resolve_weights(self.group,experiences.GROUP,defaults)['weights'],defaults)

    def test_history_restore_creates_a_new_reviewed_version_and_changes_only_future_weights(self):
        item=experiences.create(self.group,self.user,self.group_data)
        experiences.change_status(self.group,self.user,item,{'revision':1,'action':'adopt','confirmed':True})
        experiences.change_status(self.group,self.user,item,{'revision':2,'action':'pause','confirmed':True})
        self.assertEqual([v['revision'] for v in experiences.versions(self.group,item)],[3,2,1])
        client=APIClient();client.force_authenticate(self.user)
        url=f'/api/v1/experiences/{item.id}/'
        response=client.get(url+'versions/',{'group':self.group.id})
        self.assertEqual(response.status_code,200)
        self.assertEqual([v['snapshot']['status'] for v in response.data['data']],['paused','active','pending'])
        response=client.post(url+'action/',{'group':self.group.id,'revision':3,'action':'restore','version':2,'confirmed':True},format='json')
        self.assertEqual(response.status_code,200);self.assertEqual(response.data['data']['revision'],4)
        self.assertEqual(response.data['data']['status'],'active')
        effective=experiences.resolve_weights(self.group,experiences.GROUP,{'skill_coverage':0.2})
        self.assertEqual(effective['weights']['skill_coverage'],0.8)
        self.assertEqual(effective['applied'][0]['revision'],4)
        item.refresh_from_db();history=experiences.versions(self.group,item)
        self.assertEqual([v['revision'] for v in history],[4,3,2,1])
        self.assertEqual(history[2]['snapshot']['status'],'active')
        self.assertEqual(client.post(url+'action/',{'group':self.group.id,'revision':3,'action':'restore','version':1,'confirmed':True},format='json').status_code,409)
        self.assertEqual(client.get(url+'versions/',{'group':self.group_other.id}).status_code,403)

    def test_old_incomplete_audit_does_not_invent_historical_versions(self):
        item=AllocationExperience.objects.create(group=self.group,created_by=self.user,revision=7,status='active',**self.group_data)
        self.assertEqual([v['revision'] for v in experiences.versions(self.group,item)],[7])
        with self.assertRaises(BusinessError):
            experiences.change_status(self.group,self.user,item,{'revision':7,'action':'restore','version':6,'confirmed':True})
        experiences.change_status(self.group,self.user,item,{'revision':7,'action':'pause','confirmed':True})
        self.assertEqual([v['revision'] for v in experiences.versions(self.group,item)],[8,7])

    def test_edit_active_experience_requires_fresh_approval_and_retains_versions(self):
        item=experiences.create(self.group,self.user,self.group_data)
        experiences.change_status(self.group,self.user,item,{'revision':1,'action':'adopt','confirmed':True})
        client=APIClient();client.force_authenticate(self.user)
        url=f'/api/v1/experiences/{item.id}/action/'
        data={'group':self.group.id,'revision':2,'action':'edit','confirmed':True,
              'title':'修改后的技能偏好','description':'核对后的配置','target':'skill_coverage','weight':0.6}
        response=client.post(url,data,format='json')
        self.assertEqual(response.status_code,200);self.assertEqual(response.data['data']['status'],'pending')
        self.assertEqual(experiences.resolve_weights(self.group,experiences.GROUP,{'skill_coverage':0.2})['weights']['skill_coverage'],0.2)
        item.refresh_from_db();self.assertEqual([v['revision'] for v in experiences.versions(self.group,item)],[3,2,1])
        self.assertEqual(client.post(url,{**data,'revision':2},format='json').status_code,409)
        accepted=client.post(url,{'group':self.group.id,'revision':3,'action':'adopt','confirmed':True},format='json')
        self.assertEqual(accepted.status_code,200)
        self.assertEqual(experiences.resolve_weights(self.group,experiences.GROUP,{'skill_coverage':0.2})['weights']['skill_coverage'],0.6)

    def test_bed_experience_history_restore_is_separate_from_group_preferences(self):
        item=experiences.create(self.group,self.user,self.bed_data)
        experiences.change_status(self.group,self.user,item,{'revision':1,'action':'adopt','confirmed':True})
        experiences.change_status(self.group,self.user,item,{'revision':2,'action':'pause','confirmed':True})
        with self.assertRaises(BusinessError):
            experiences.change_status(self.group,self.user,item,{'revision':3,'action':'restore','version':2})
        experiences.change_status(self.group,self.user,item,{'revision':3,'action':'restore','version':2,'confirmed':True})
        bed=experiences.resolve_weights(self.group,experiences.BED,{'load_balance':0.1})
        group=experiences.resolve_weights(self.group,experiences.GROUP,{'skill_coverage':0.1})
        self.assertEqual(bed['weights']['load_balance'],0.7)
        self.assertEqual(bed['applied'][0]['revision'],4)
        self.assertEqual(group['applied'],[])

    def test_one_time_and_two_business_scopes_are_independent(self):
        group=experiences.create(self.group,self.user,self.group_data)
        bed=experiences.create(self.group,self.user,self.bed_data)
        experiences.change_status(self.group,self.user,bed,{'revision':1,'action':'adopt','confirmed':True})
        result=experiences.resolve_weights(self.group,experiences.GROUP,{'skill_coverage':0.2},[group.id])
        self.assertEqual(result['weights']['skill_coverage'],0.8);self.assertTrue(result['applied'][0]['one_time'])
        self.assertEqual(AllocationExperience.objects.get(pk=group.id).status,'pending')
        self.assertEqual(experiences.resolve_weights(self.group,experiences.BED,{'load_balance':0.2})['weights']['load_balance'],0.7)
        self.assertEqual(experiences.resolve_weights(self.group_other,experiences.BED,{'load_balance':0.2})['applied'],[])
        with self.assertRaises(BusinessError):experiences.resolve_weights(self.group,experiences.GROUP,{'skill_coverage':0.2},[bed.id])

    def test_conflicting_targets_have_auditable_precedence(self):
        first=experiences.create(self.group,self.user,self.group_data)
        second=experiences.create(self.group,self.user,{**self.group_data,'weight':0.9})
        for item in (first,second):experiences.change_status(self.group,self.user,item,{'revision':1,'action':'adopt','confirmed':True})
        result=experiences.resolve_weights(self.group,experiences.GROUP,{'skill_coverage':0.1})
        self.assertEqual(result['weights']['skill_coverage'],0.9)
        self.assertEqual(result['applied'][0]['id'],second.id);self.assertEqual(result['excluded'][0]['id'],first.id)

    def test_model_suggestion_needs_consent_and_remains_pending(self):
        model_output={'title':'技能覆盖','description':'尽量改善技能覆盖','target':'skill_coverage','weight':0.9}
        payload={'business_type':experiences.GROUP,'text':'尽量改善技能覆盖','synthetic':True,'consent':True}
        with patch('scheduling.services.experiences.public_config',return_value={'configured':True,'model':'test-model','provider':'deepseek','base_url':'https://api.deepseek.com','revision':1}), \
             patch('scheduling.services.experiences.secret_value',return_value='synthetic-key'), \
             patch('scheduling.services.experiences.chat_completion',return_value=json.dumps(model_output)) as chat:
            item=experiences.suggest(self.group,self.user,payload)
            self.assertEqual(item.source,'ai');self.assertEqual(item.status,'pending');self.assertEqual(chat.call_count,1)
            with self.assertRaises(BusinessError):experiences.suggest(self.group,self.user,{**payload,'consent':False})
            with self.assertRaises(BusinessError):experiences.suggest(self.group,self.user,{**payload,'text':'电话 13812345678'})
            self.assertEqual(chat.call_count,1)

    def test_feedback_is_scoped_and_never_infers_reasons(self):
        item=experiences.record_feedback(self.group,self.user,{'business_type':experiences.BED,
            'before':{str(self.bed.id):self.nurse.id},'after':{str(self.bed.id):None},'adopted':True},verified_adoption=True)
        self.assertEqual(item.reason,'');self.assertTrue(item.adopted)
        outsider=Bed.objects.create(group=self.group_other,number=1)
        with self.assertRaises(BusinessError):experiences.record_feedback(self.group,self.user,{'business_type':experiences.BED,
            'before':{str(outsider.id):self.nurse.id},'after':{}})

    def test_non_supported_target_cannot_be_converted_into_a_hard_rule(self):
        with self.assertRaises(BusinessError):experiences.create(self.group,self.user,{**self.bed_data,'target':'ignore_qualification'})

    def test_feedback_analysis_sends_only_reason_counts_and_remains_pending(self):
        safe=experiences.record_feedback(self.group,self.user,{'business_type':experiences.BED,
            'before':{str(self.bed.id):self.nurse.id},'after':{str(self.bed.id):None},
            'reason':'工作负荷过高','adopted':True},verified_adoption=True)
        experiences.record_feedback(self.group,self.user,{'business_type':experiences.BED,
            'before':{},'after':{},'reason':'患者姓名不可外发','adopted':True},verified_adoption=True)
        output={'title':'平衡负荷','description':'尽量平衡负荷','target':'load_balance','weight':0.8}
        with patch('scheduling.services.experiences.public_config',return_value={'configured':True,'model':'test-model','provider':'deepseek','base_url':'https://api.deepseek.com','revision':1}), \
             patch('scheduling.services.experiences.secret_value',return_value='synthetic-key'), \
             patch('scheduling.services.experiences.chat_completion',return_value=json.dumps(output)) as chat:
            item=experiences.analyze_feedback(self.group,self.user,{'business_type':experiences.BED,'synthetic':True,'consent':True})
        self.assertEqual(item.status,'pending');self.assertEqual(item.source_details['feedback_ids'],[safe.id])
        body=json.dumps(chat.call_args.args[2],ensure_ascii=False)
        self.assertIn('工作负荷过高',body);self.assertNotIn('患者姓名不可外发',body);self.assertNotIn(self.nurse.name,body)

    def test_api_rejects_cross_group_and_forged_confirmed_feedback(self):
        client=APIClient();client.force_authenticate(self.user)
        self.assertEqual(client.get('/api/v1/experiences/',{'group':self.group_other.id,'business_type':experiences.GROUP}).status_code,403)
        response=client.post('/api/v1/experiences/feedback/',{'group':self.group.id,'business_type':experiences.BED,
            'before':{str(self.bed.id):self.nurse.id},'after':{},'adopted':True},format='json')
        self.assertEqual(response.status_code,403)
        response=client.post('/api/v1/experiences/',{'group':self.group.id,**self.group_data},format='json')
        self.assertEqual(response.status_code,200);self.assertEqual(response.data['data']['status'],'pending')
