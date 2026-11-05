import copy
import json
import os
from datetime import date, timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient
from scheduling.models import AIGroupingTask, AIServiceConfiguration, CareGroup, Nurse, SchedulePublication, GroupingPublication
from scheduling.experience_models import AllocationFeedback
from scheduling.services import grouping, experiences


DAY=date(2026,10,22)


class AIGroupingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with patch('django.utils.timezone.localdate',return_value=DAY): call_command('seed_demo',verbosity=0)
        cls.group=CareGroup.objects.get(name='第3组')
        cls.manager=get_user_model().objects.get(username='group3')
        cls.head=get_user_model().objects.get(username='head_nurse')

    def setUp(self):
        self.day=patch('django.utils.timezone.localdate',return_value=DAY);self.day.start();self.addCleanup(self.day.stop)
        self.env=patch.dict(os.environ,{'AI_PERSONNEL_EXTERNAL_AUTHORIZED':'false'});self.env.start();self.addCleanup(self.env.stop)
        self.client=APIClient();self.client.force_authenticate(self.manager)
        AIServiceConfiguration.objects.create(pk=1,provider='deepseek',base_url='https://api.deepseek.com/v1',
            model='synthetic-test-model',encrypted_key='test-cipher')
        self.calls=[]
        def artificial_response(config,key,messages,**kwargs):
            data=json.loads(messages[1]['content']);self.calls.append(data)
            if 'group_count' not in data:return json.dumps({'summary':'根据已记录资料复核。','concerns':['请人工确认。']})
            people=data['nurses']
            return json.dumps({'analyses':[{'code':p['code'],'summary':'已记录资料供人工核对。'} for p in people],
                'candidate_groups':{p['code']:(i%data['group_count'])+1 for i,p in enumerate(people)},
                'complementarity':['技能配置供核对。'],'teaching':['带教需核实。'],'concerns':['能力能级未排序。']})
        self.fake=patch('scheduling.services.grouping.chat_completion',side_effect=artificial_response);self.fake.start();self.addCleanup(self.fake.stop)
        self.key=patch('scheduling.services.grouping.secret_value',return_value='synthetic-key');self.key.start();self.addCleanup(self.key.stop)
        self.params={'group':self.group.id,'group_count':4,'group_sizes':[3,3,3,3],'min_leaders':1,
            'mentor_ids':[],'locked':{},'effective_date':str(DAY),'synthetic':True,'consent':True,'junior_years':2}

    def create(self,**values):
        return self.client.post('/api/v1/grouping/',{**self.params,**values},format='json')

    def test_missing_configuration_never_fakes_real_analysis(self):
        AIServiceConfiguration.objects.all().delete()
        response=self.create();self.assertEqual(response.status_code,503)
        self.assertEqual(AIGroupingTask.objects.count(),0);self.assertFalse(self.calls)

    def test_real_personnel_requires_server_authorization_and_explicit_consent(self):
        self.assertEqual(self.create(synthetic=False).status_code,403)
        self.assertEqual(self.create(consent=False).status_code,400)
        self.assertFalse(self.calls)

    def test_three_solutions_from_actual_model_path_and_no_identity_in_prompt(self):
        response=self.create();self.assertEqual(response.status_code,200)
        data=response.data['data'];self.assertEqual([p['code'] for p in data['plans']],['A','B','C'])
        self.assertEqual(len(data['participants']),12);self.assertEqual(len(data['excluded']),4)
        self.assertEqual(data['parameters']['junior_years'],2)
        self.assertIn('≤2年',data['metric_definitions']['mentoring_support'])
        self.assertTrue(all(p['validation']['valid'] for p in data['plans']))
        raw=json.dumps(self.calls[0],ensure_ascii=False)
        for person in data['participants']:
            self.assertNotIn(person['name'],raw);self.assertNotIn(person['employee_id'],raw)
        self.assertNotIn('name',self.calls[0]['nurses'][0])

    def test_bad_model_output_creates_no_task(self):
        with patch('scheduling.services.grouping.chat_completion',return_value='{"analyses": []}'):
            self.assertEqual(self.create().status_code,502)
        self.assertFalse(AIGroupingTask.objects.exists())

    def test_invalid_sizes_dates_and_leaders_are_rejected(self):
        for values in [{'group_sizes':[3,3,3,2]}, {'effective_date':str(DAY+timedelta(days=1))},
                       {'min_leaders':2}, {'junior_years':True}]:
            with self.subTest(values=values):self.assertIn(self.create(**values).status_code,[400,422])
        self.assertFalse(self.calls)

    def test_explicit_pair_constraints_are_solved_and_rechecked_after_edit(self):
        people=grouping.collect(self.group)[0]
        free=[p['id'] for p in people if not p['is_leader']]
        same,avoid=free[:2],free[2:4]
        response=self.create(same_pairs=[same],avoid_pairs=[avoid])
        self.assertEqual(response.status_code,200)
        result=response.data['data']
        for plan in result['plans']:
            self.assertEqual(plan['assignments'][str(same[0])],plan['assignments'][str(same[1])])
            self.assertNotEqual(plan['assignments'][str(avoid[0])],plan['assignments'][str(avoid[1])])
        plan=result['plans'][0]
        altered=copy.deepcopy(plan['assignments'])
        altered[str(same[0])]=1 if altered[str(same[1])]!=1 else 2
        report=grouping.validate(result['participants'],result['parameters'],altered,plan['leaders'])
        self.assertFalse(report['valid'])
        self.assertTrue(any('指定同组' in item['message'] for item in report['errors']))
        self.assertEqual(self.create(same_pairs=[same],avoid_pairs=[same]).status_code,400)

    def test_review_and_confirm_preserve_published_history(self):
        original=copy.deepcopy(SchedulePublication.objects.get().snapshot)
        task=self.create().data['data'];plan=task['plans'][0]
        assignments=copy.deepcopy(plan['assignments']);leaders=copy.deepcopy(plan['leaders'])
        by_id={p['id']:p for p in task['participants']}
        free=[p for p in task['participants'] if not p['is_leader']]
        first=free[0];second=next(p for p in free if assignments[str(p['id'])]!=assignments[str(first['id'])])
        assignments[str(first['id'])],assignments[str(second['id'])]=assignments[str(second['id'])],assignments[str(first['id'])]
        url=f"/api/v1/grouping/{task['id']}/"
        payload={'group':self.group.id,'revision':task['revision'],'assignments':assignments,'leaders':leaders,'confirmed':True}
        self.assertEqual(self.client.post(url+'confirm/',payload,format='json').status_code,409)
        review=self.client.post(url+'review/',{**payload,'ai_review':True,'synthetic':True,'consent':True},format='json')
        self.assertEqual(review.status_code,200);result=review.data['data']
        self.assertTrue(result['reviewed']['validation']['valid']);self.assertTrue(result['reviewed']['ai_review']['summary'])
        response=self.client.post(url+'confirm/',{**payload,'revision':result['revision']},format='json')
        self.assertEqual(response.status_code,200);self.assertEqual(response.data['data']['status'],'confirmed')
        self.assertEqual(Nurse.objects.get(pk=first['id']).subgroup,assignments[str(first['id'])])
        self.assertEqual(SchedulePublication.objects.get().snapshot,original)
        self.assertEqual(self.client.post(url+'confirm/',{**payload,'revision':response.data['data']['revision']},format='json').status_code,409)

    def test_invalid_manual_move_or_lock_conflict_cannot_confirm(self):
        nurse=Nurse.objects.filter(base_type='rotation').first()
        task=self.create(locked={str(nurse.id):nurse.subgroup}).data['data'];plan=task['plans'][0]
        wrong=copy.deepcopy(plan['assignments']);wrong[str(nurse.id)]=nurse.subgroup%4+1
        url=f"/api/v1/grouping/{task['id']}/review/"
        response=self.client.post(url,{'group':self.group.id,'revision':task['revision'],
            'assignments':wrong,'leaders':plan['leaders'],'ai_review':False},format='json')
        self.assertEqual(response.status_code,422)

    def test_concurrent_nurse_change_rejects_stale_review_and_permissions(self):
        task=self.create().data['data'];plan=task['plans'][0]
        n=Nurse.objects.get(pk=task['participants'][0]['id']);n.years+=1;n.revision+=1;n.save()
        url=f"/api/v1/grouping/{task['id']}/review/"
        body={'group':self.group.id,'revision':task['revision'],'assignments':plan['assignments'],'leaders':plan['leaders']}
        self.assertEqual(self.client.post(url,body,format='json').status_code,409)
        self.client.force_authenticate(self.head)
        self.assertEqual(self.client.post('/api/v1/grouping/',self.params,format='json').status_code,403)

    def test_zero_defined_juniors_does_not_create_a_fictitious_mentor_score(self):
        task=self.create(junior_years=0,mentor_ids=[Nurse.objects.filter(is_leader=True).first().id]).data['data']
        self.assertTrue(all(plan['metrics']['mentoring_support'] is None for plan in task['plans']))

    def test_weights_are_saved_normalized_and_bad_values_rejected(self):
        weights={'level_balance':0.0,'years_balance':0.7,'skill_coverage':0.9,'mentoring_support':0.8,'stability':0.5}
        response=self.create(weights=weights);self.assertEqual(response.status_code,200)
        data=response.data['data'];self.assertEqual(data['parameters']['requested_weights'],weights)
        self.assertEqual(data['parameters']['weights']['stability'],0.0)
        for plan in data['plans']:
            self.assertEqual(plan['normalization']['level_balance']['coefficient'],0)
            self.assertEqual(plan['effective_weights']['stability'],0.0)
            self.assertIn(plan['solver_status'],['optimal','feasible'])
            self.assertTrue(plan['validation']['valid'])
        for invalid in [True,1.1,-0.1,0.25,'0.5']:
            self.assertEqual(self.create(weights={**weights,'years_balance':invalid}).status_code,400)

    def test_assessment_preference_enters_solver_without_inventing_missing_levels(self):
        nurses=list(Nurse.objects.filter(group=self.group,base_type='rotation').order_by('id'))
        for index,nurse in enumerate(nurses):
            nurse.assessment_level=([1,2,3,None][index%4]);nurse.save(update_fields=['assessment_level'])
        weights={**grouping.DEFAULT_WEIGHTS,'assessment_balance':0.9}
        result=self.create(weights=weights).data['data']
        for plan in result['plans']:
            self.assertEqual(plan['normalization']['assessment_balance']['coefficient'],9)
            self.assertEqual(plan['metrics']['assessment_missing'],3)
            self.assertEqual(sum(v['1'] for v in plan['metrics']['assessment_distribution'].values()),3)
            self.assertTrue(plan['validation']['valid'])
        self.assertIn('1档为最高',grouping.METRIC_DEFINITIONS['assessment_balance'])

    def test_active_assessment_experience_is_applied_but_pause_excludes_it(self):
        nurse=Nurse.objects.filter(group=self.group,base_type='rotation').first()
        nurse.assessment_level=1;nurse.save(update_fields=['assessment_level'])
        item=experiences.create(self.group,self.manager,{'business_type':experiences.GROUP,'title':'评估能级均衡',
            'description':'依据已记录能级均衡配置','target':'assessment_balance','weight':0.8})
        experiences.change_status(self.group,self.manager,item,{'revision':item.revision,'action':'adopt','confirmed':True})
        task=self.create().data['data']
        self.assertEqual(task['parameters']['weights']['assessment_balance'],0.8)
        self.assertEqual(task['plans'][0]['normalization']['assessment_balance']['coefficient'],8)
        experiences.change_status(self.group,self.manager,item,{'revision':item.revision,'action':'pause','confirmed':True})
        task=self.create().data['data']
        self.assertEqual(task['parameters']['weights']['assessment_balance'],0.0)

    def test_formal_clear_and_restore_preserves_qualifications_and_history(self):
        task=self.create().data['data'];plan=task['plans'][0];base=f"/api/v1/grouping/{task['id']}/"
        body={'group':self.group.id,'revision':task['revision'],'assignments':plan['assignments'],'leaders':plan['leaders']}
        reviewed=self.client.post(base+'review/',body,format='json').data['data']
        self.assertEqual(self.client.post(base+'confirm/',{**body,'revision':reviewed['revision'],'confirmed':True},format='json').status_code,200)
        original=copy.deepcopy(SchedulePublication.objects.get().snapshot)
        before={n.id:(n.subgroup,n.is_leader,n.base_type) for n in Nurse.objects.filter(group=self.group)}
        state=self.client.get('/api/v1/grouping/formal/',{'group':self.group.id}).data['data']
        cleared=self.client.post('/api/v1/grouping/formal/undo/',{'group':self.group.id,'mode':'clear',
            'confirmed':True,'fingerprint':state['fingerprint'],'reason':'重新配置'},format='json')
        self.assertEqual(cleared.status_code,200);self.assertEqual(cleared.data['data']['assigned_count'],0)
        self.assertTrue(cleared.data['data']['can_restore'])
        for n in Nurse.objects.filter(group=self.group):
            self.assertIsNone(n.subgroup);self.assertEqual((n.is_leader,n.base_type),before[n.id][1:])
        restored=self.client.post('/api/v1/grouping/formal/undo/',{'group':self.group.id,'mode':'restore',
            'confirmed':True,'fingerprint':cleared.data['data']['fingerprint']},format='json')
        self.assertEqual(restored.status_code,200)
        self.assertEqual({n.id:n.subgroup for n in Nurse.objects.filter(group=self.group)}, {k:v[0] for k,v in before.items()})
        self.assertEqual(SchedulePublication.objects.get().snapshot,original)
        self.assertEqual(GroupingPublication.objects.filter(group=self.group).count(),4)
        self.assertEqual(self.client.post('/api/v1/grouping/formal/undo/',{'group':self.group.id,'mode':'clear',
            'confirmed':True,'fingerprint':state['fingerprint']},format='json').status_code,409)

    def test_invalid_historical_leader_blocks_restore(self):
        task=self.create().data['data'];plan=task['plans'][0];base=f"/api/v1/grouping/{task['id']}/"
        body={'group':self.group.id,'revision':task['revision'],'assignments':plan['assignments'],'leaders':plan['leaders']}
        reviewed=self.client.post(base+'review/',body,format='json').data['data']
        self.client.post(base+'confirm/',{**body,'revision':reviewed['revision'],'confirmed':True},format='json')
        state=self.client.get('/api/v1/grouping/formal/',{'group':self.group.id}).data['data']
        cleared=self.client.post('/api/v1/grouping/formal/undo/',{'group':self.group.id,'mode':'clear',
            'confirmed':True,'fingerprint':state['fingerprint']},format='json').data['data']
        leader=Nurse.objects.get(pk=next(iter(plan['leaders'].values())));leader.is_leader=False;leader.save()
        state=self.client.get('/api/v1/grouping/formal/',{'group':self.group.id}).data['data']
        self.assertFalse(state['can_restore']);self.assertTrue(state['restore_issues'])
        self.assertEqual(self.client.post('/api/v1/grouping/formal/undo/',{'group':self.group.id,'mode':'restore',
            'confirmed':True,'fingerprint':state['fingerprint']},format='json').status_code,422)

    def test_confirmed_experience_affects_solver_and_changed_preference_stales_task(self):
        exp=experiences.create(self.group,self.manager,{'business_type':experiences.GROUP,'title':'平衡技能',
            'description':'提升覆盖','target':'skill_coverage','weight':0.2})
        experiences.change_status(self.group,self.manager,exp,{'action':'adopt','revision':1,'confirmed':True})
        task=self.create().data['data'];plan=task['plans'][0]
        self.assertEqual(task['parameters']['requested_weights']['skill_coverage'],0.9)
        self.assertEqual(task['parameters']['weights']['skill_coverage'],0.2)
        self.assertEqual(plan['normalization']['skill_coverage']['coefficient'],2)
        self.assertEqual(task['parameters']['applied_experiences'][0]['id'],exp.id)
        experiences.change_status(self.group,self.manager,exp,{'action':'pause','revision':2,'confirmed':True})
        url=f"/api/v1/grouping/{task['id']}/review/"
        self.assertEqual(self.client.post(url,{'group':self.group.id,'revision':task['revision'],
            'assignments':plan['assignments'],'leaders':plan['leaders']},format='json').status_code,409)

    def test_verified_manual_adjustment_feedback_only_adopted_after_confirm(self):
        task=self.create().data['data'];plan=task['plans'][0];assignments=copy.deepcopy(plan['assignments'])
        members=[p for p in task['participants'] if not p['is_leader']]
        first=members[0];second=next(p for p in members if assignments[str(p['id'])]!=assignments[str(first['id'])])
        assignments[str(first['id'])],assignments[str(second['id'])]=assignments[str(second['id'])],assignments[str(first['id'])]
        base=f"/api/v1/grouping/{task['id']}/";payload={'group':self.group.id,'revision':task['revision'],
            'assignments':assignments,'leaders':plan['leaders'],'reference_assignments':plan['assignments'],
            'adjustment_reason':'技能或带教搭配需要调整'}
        result=self.client.post(base+'review/',payload,format='json');self.assertEqual(result.status_code,200)
        self.assertEqual(list(AllocationFeedback.objects.values_list('adopted',flat=True)),[False])
        result=self.client.post(base+'confirm/',{**payload,'revision':result.data['data']['revision'],'confirmed':True},format='json')
        self.assertEqual(result.status_code,200)
        self.assertEqual(list(AllocationFeedback.objects.order_by('id').values_list('adopted',flat=True)),[False,True])

    def test_leader_only_adjustment_is_recorded_and_adopted_after_confirmation(self):
        extra=Nurse.objects.filter(group=self.group,base_type='rotation',is_leader=False).first()
        extra.is_leader=True;extra.save(update_fields=['is_leader'])
        task=self.create().data['data'];plan=task['plans'][0]
        members=task['participants'];assignments=plan['assignments'];leaders=copy.deepcopy(plan['leaders'])
        candidates=[p for p in members if p['is_leader'] and
            leaders.get(str(assignments[str(p['id'])]))!=p['id']]
        self.assertTrue(candidates)
        person=candidates[0];subgroup=str(assignments[str(person['id'])]);leaders[subgroup]=person['id']
        base=f"/api/v1/grouping/{task['id']}/";payload={'group':self.group.id,'revision':task['revision'],
            'assignments':assignments,'leaders':leaders,'reference_assignments':plan['assignments'],
            'reference_leaders':plan['leaders'],'adjustment_reason':'带组老师配置不合理'}
        reviewed=self.client.post(base+'review/',payload,format='json');self.assertEqual(reviewed.status_code,200)
        pending=AllocationFeedback.objects.get(adopted=False)
        self.assertEqual(pending.before,pending.after)
        self.assertEqual(pending.context,{'before_leaders':plan['leaders'],'after_leaders':leaders})
        confirmed=self.client.post(base+'confirm/',{**payload,'revision':reviewed.data['data']['revision'],'confirmed':True},format='json')
        self.assertEqual(confirmed.status_code,200)
        self.assertEqual(AllocationFeedback.objects.get(adopted=True).context,pending.context)
