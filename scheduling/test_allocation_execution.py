"""Integration tests use the Django test database and synthetic model responses."""
import copy
import json
import os
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from scheduling.models import AccountProfile, BedDraft, BedPublication, CareGroup, Nurse, Patient, SchedulePublication
from scheduling.services import allocation_execution, beds
from scheduling.services.common import BusinessError
from scheduling.task_models import AllocationConversation, AllocationExecution
from scheduling.experience_models import AllocationFeedback
from scheduling.services import experiences


DAY=date(2026,10,22)
CONFIG={'configured':True,'provider':'deepseek','base_url':'https://api.deepseek.com',
        'model':'synthetic-test-model','hospital_authorized':False}


class AllocationExecutionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with patch('django.utils.timezone.localdate',return_value=DAY):
            call_command('seed_demo',verbosity=0)
        cls.group=CareGroup.objects.get(name='第3组')
        cls.manager=get_user_model().objects.get(username='group3')
        cls.head=get_user_model().objects.get(username='head_nurse')
        cls.draft=BedDraft.objects.get(group=cls.group,date=DAY,shift='A')

    def setUp(self):
        self.client=APIClient();self.client.force_authenticate(self.manager)
        env=patch.dict(os.environ,{'AI_PERSONNEL_EXTERNAL_AUTHORIZED':'false','AI_HOSPITAL_AUTHORIZED':'false'})
        env.start();self.addCleanup(env.stop)
        day=patch('django.utils.timezone.localdate',return_value=DAY);day.start();self.addCleanup(day.stop)
        self.group_args={'kind':'GROUP_ASSIGNMENT','group_count':4,'group_sizes':[3,3,3,3],
            'min_leaders':1,'junior_years':2,'mentor_ids':[],'locked':{},'effective_date':str(DAY),
            'synthetic':True,'consent':True}
        self.bed_args={'kind':'BED_ASSIGNMENT','id':self.draft.id,'revision':self.draft.revision,
            'fingerprint':beds.fingerprint(self.draft),'synthetic':True,'consent':True}

    @staticmethod
    def fake_group_model(config,key,messages,**kwargs):
        data=json.loads(messages[1]['content']); people=data['nurses']
        return json.dumps({'analyses':[{'code':p['code'],'summary':'合成资料待人工核对。'} for p in people],
            'candidate_groups':{p['code']:(i%data['group_count'])+1 for i,p in enumerate(people)},
            'complementarity':['合成技能组合供核查。'],'teaching':['带教资格须核对。'],'concerns':['能力能级未排序。']})

    def create(self,params):
        return allocation_execution.start(self.group,self.manager,params['kind'],params,dispatch=False)

    def test_group_task_seven_real_stages_and_candidate_does_not_publish(self):
        before=list(Nurse.objects.filter(group=self.group).order_by('id').values_list('id','subgroup'))
        schedule_before=copy.deepcopy(SchedulePublication.objects.get(group=self.group).snapshot)
        task=self.create(self.group_args)
        with patch('scheduling.services.grouping.public_config',return_value=CONFIG), \
             patch('scheduling.services.grouping.secret_value',return_value='synthetic-key'), \
             patch('scheduling.services.grouping.chat_completion',side_effect=self.fake_group_model) as model:
            allocation_execution.run(task.pk)
        task.refresh_from_db();self.assertEqual(task.status,'completed');self.assertEqual(model.call_count,1)
        completed=list(task.events.filter(status='completed').values_list('stage',flat=True))
        self.assertEqual(completed,[1,2,3,4,5,6,7])
        first=task.events.get(stage=1,status='completed').summary
        self.assertEqual(sum(first['levels'].values()),first['participants'])
        self.assertEqual(sum(first['years'].values()),first['participants'])
        self.assertTrue(task.result.get('grouping_task_id'))
        self.assertEqual(list(Nurse.objects.filter(group=self.group).order_by('id').values_list('id','subgroup')),before)
        self.assertEqual(SchedulePublication.objects.get(group=self.group).snapshot,schedule_before)

    def test_bed_task_real_stages_model_analysis_and_candidate_only(self):
        before_draft=copy.deepcopy(self.draft.assignments)
        published_before=BedPublication.objects.count()
        task=self.create(self.bed_args)
        with patch('scheduling.services.ai_configuration.public_config',return_value=CONFIG), \
             patch('scheduling.services.ai_configuration.secret_value',return_value='synthetic-key'), \
             patch('scheduling.services.ai_configuration.chat_completion',return_value='{"summary":"请核对资格和负荷。"}') as model:
            allocation_execution.run(task.pk)
        task.refresh_from_db();self.assertEqual(task.status,'completed',task.error)
        self.assertEqual(list(task.events.filter(status='completed').values_list('stage',flat=True)),[1,2,3,4,5,6,7])
        self.assertEqual(model.call_count,1);self.assertTrue(task.result['validation']['valid'])
        prompt=model.call_args.args[2][1]['content']
        for patient in Patient.objects.filter(group=self.group):
            self.assertNotIn(patient.name,prompt);self.assertNotIn(patient.code,prompt)
        self.draft.refresh_from_db();self.assertEqual(self.draft.assignments,before_draft)
        self.assertEqual(BedPublication.objects.count(),published_before)

    def test_bed_ai_invalid_response_preserves_failed_stage_and_no_candidate(self):
        initial=copy.deepcopy(self.draft.assignments);published=BedPublication.objects.count()
        task=self.create(self.bed_args)
        with patch('scheduling.services.ai_configuration.public_config',return_value=CONFIG), \
             patch('scheduling.services.ai_configuration.secret_value',return_value='synthetic-key'), \
             patch('scheduling.services.ai_configuration.chat_completion',return_value='{"summary":null}'):
            allocation_execution.run(task.pk)
        task.refresh_from_db();self.assertEqual(task.status,'failed')
        self.assertIn('格式无效',task.error)
        self.assertEqual(list(task.events.filter(status='completed').values_list('stage',flat=True)),[1,2,3])
        self.assertTrue(task.events.filter(stage=4,status='failed').exists())
        self.assertFalse(task.events.filter(stage=5).exists())
        self.assertEqual(task.result,{})
        self.draft.refresh_from_db();self.assertEqual(self.draft.assignments,initial)
        self.assertEqual(BedPublication.objects.count(),published)

    def test_duplicate_active_task_fails_but_different_kind_allowed(self):
        task=self.create(self.group_args)
        with self.assertRaises(BusinessError) as error:self.create(self.group_args)
        self.assertEqual(error.exception.status_code,409)
        other=self.create(self.bed_args);self.assertNotEqual(other.pk,task.pk)
        AllocationExecution.objects.filter(pk=task.pk).update(status='failed')
        self.assertTrue(self.create(self.group_args).pk)

    def test_task_access_is_group_scoped_and_read_only_role_cannot_start(self):
        other=CareGroup.objects.create(name='隔离测试组')
        user=get_user_model().objects.create_user('manager-other',password='synthetic')
        AccountProfile.objects.create(user=user,role='group_manager',group=other)
        # This test checks authorization for an existing foreign task. The
        # start service now validates and snapshots real participants, so an
        # empty group cannot start a valid allocation task.
        task=AllocationExecution.objects.create(group=other,created_by=user,kind='GROUP_ASSIGNMENT',parameters={})
        self.assertEqual(self.client.get(f'/api/v1/allocation-tasks/{task.pk}/').status_code,404)
        self.assertEqual(self.client.get(f'/api/v1/allocation-tasks/{task.pk}/',{'group':other.id}).status_code,403)
        self.client.force_authenticate(self.head)
        self.assertEqual(self.client.post('/api/v1/allocation-tasks/',{'group':self.group.id,**self.group_args},format='json').status_code,403)

    def test_task_requires_explicit_consent_and_draft_revision(self):
        response=self.client.post('/api/v1/allocation-tasks/',{'group':self.group.id,**self.group_args,'consent':False},format='json')
        self.assertEqual(response.status_code,400);self.assertFalse(AllocationExecution.objects.exists())
        response=self.client.post('/api/v1/allocation-tasks/',{'group':self.group.id,**self.bed_args,'revision':self.draft.revision+1},format='json')
        self.assertEqual(response.status_code,409);self.assertFalse(AllocationExecution.objects.exists())

    def test_successful_api_start_schedules_exactly_one_background_runner_on_commit(self):
        with patch('scheduling.services.allocation_execution.Thread') as worker:
            with self.captureOnCommitCallbacks(execute=True):
                response=self.client.post('/api/v1/allocation-tasks/',
                    {'group':self.group.id,**self.group_args},format='json')
            self.assertEqual(response.status_code,200)
            task_id=response.data['data']['id']
            worker.assert_called_once()
            self.assertIs(worker.call_args.kwargs['target'],allocation_execution.run)
            self.assertEqual(worker.call_args.kwargs['args'],(task_id,''))
            worker.return_value.start.assert_called_once()
            self.assertEqual(AllocationExecution.objects.get(pk=task_id).status,'queued')

    def test_event_cursor_returns_only_new_events(self):
        task=self.create(self.group_args)
        allocation_execution.event(task,1,'running',{'count':1})
        allocation_execution.event(task,1,'completed',{'count':2})
        first=task.events.first().id
        response=self.client.get(f'/api/v1/allocation-tasks/{task.pk}/',{'group':self.group.id,'after':first})
        self.assertEqual(response.status_code,200)
        self.assertEqual([e['status'] for e in response.data['data']['events']],['completed'])

    def test_expired_task_is_failed_cannot_append_events_and_allows_retry(self):
        task=self.create(self.group_args)
        AllocationExecution.objects.filter(pk=task.pk).update(status='running',created_at=timezone.now()-timedelta(minutes=4))
        replacement=self.create(self.group_args)
        task.refresh_from_db();self.assertEqual(task.status,'failed')
        self.assertIn('无进展',task.error)
        self.assertTrue(task.events.filter(status='failed').exists())
        count=task.events.count()
        with self.assertRaises(BusinessError) as error:
            allocation_execution.event(task,5,'completed',{'invalid':'stale'})
        self.assertEqual(error.exception.status_code,409)
        self.assertEqual(task.events.count(),count)
        allocation_execution.run(task.pk)
        task.refresh_from_db();self.assertEqual(task.status,'failed');self.assertEqual(task.result,{})
        self.assertNotEqual(task.id,replacement.id)

    def test_recent_progress_prevents_expiration(self):
        task=self.create(self.group_args)
        AllocationExecution.objects.filter(pk=task.pk).update(status='running',created_at=timezone.now()-timedelta(minutes=4))
        allocation_execution.event(task,4,'running',{'message':'合成测试'})
        with self.assertRaises(BusinessError) as error:self.create(self.group_args)
        self.assertEqual(error.exception.status_code,409)
        task.refresh_from_db();self.assertEqual(task.status,'running')

    def test_expired_worker_cannot_commit_result(self):
        task=self.create(self.group_args)
        def interrupted(group,user,parameters,progress,**kwargs):
            AllocationExecution.objects.filter(pk=task.pk).update(created_at=timezone.now()-timedelta(minutes=4))
            allocation_execution.expire_stale(group,'GROUP_ASSIGNMENT')
            class FakeResult: pk=123
            return FakeResult()
        with patch('scheduling.services.grouping.create_task',side_effect=interrupted):
            allocation_execution.run(task.pk)
        task.refresh_from_db();self.assertEqual(task.status,'failed');self.assertEqual(task.result,{})

    def test_confirmed_bed_experience_changes_solver_coefficient_and_pause_restores_default(self):
        item=experiences.create(self.group,self.manager,{'business_type':'BED_ASSIGNMENT','title':'合成负荷偏好',
            'description':'测试软目标权重','target':'load_balance','weight':0.2})
        experiences.change_status(self.group,self.manager,item,{'revision':item.revision,'action':'adopt','confirmed':True})
        recorded=[];actual_solve=beds.cp_model.CpSolver.Solve
        def inspect_objective(solver,model):
            proto=model.Proto()
            recorded.append({proto.variables[index].name:coefficient for index,coefficient in zip(proto.objective.vars,proto.objective.coeffs)})
            return actual_solve(solver,model)
        with patch.object(beds.cp_model.CpSolver,'Solve',inspect_objective):
            result=beds.recommend(self.draft)
            self.assertEqual(result['effective_weights']['load_balance'],0.2)
            self.assertEqual(recorded[-1]['normalized_load_balance'],2)
            self.assertTrue(any(entry['id']==item.id for entry in result['applied_experiences']))
            item.refresh_from_db()
            experiences.change_status(self.group,self.manager,item,{'revision':item.revision,'action':'pause','confirmed':True})
            result=beds.recommend(self.draft)
        self.assertEqual(result['effective_weights']['load_balance'],1.0)
        self.assertEqual(recorded[-1]['normalized_load_balance'],10)
        self.assertFalse(any(entry['id']==item.id for entry in result['applied_experiences']))

    def test_manual_adjustment_feedback_only_adopted_after_matching_publication(self):
        original=copy.deepcopy(self.draft.assignments)
        _,nurses=beds.on_duty(self.group,DAY,'A')
        alternative=None
        for bed_id,old in original.items():
            for nurse in nurses:
                candidate={**original,bed_id:nurse.id}
                if nurse.id!=old and beds.validate(self.draft,candidate)['valid']:
                    alternative=candidate;break
            if alternative:break
        self.assertIsNotNone(alternative,'合成数据须包含可合法调整的床位')
        def context():
            self.draft.refresh_from_db();self.group.refresh_from_db();self.draft.group=self.group
            return {'revision':self.draft.revision,'fingerprint':beds.fingerprint(self.draft)}
        beds.modify(self.draft,self.manager,{**context(),'assignments':alternative,'reason':'护理连续性考虑'},record_adjustment=True)
        pending=AllocationFeedback.objects.get(business_type='BED_ASSIGNMENT',adopted=False)
        self.assertEqual(pending.before,original);self.assertEqual(pending.after,alternative)
        changed={int(k) for k in original if original.get(k)!=alternative.get(k)}
        self.assertEqual({row['bed_id'] for row in pending.context['patient_tasks']},changed)
        for row in pending.context['patient_tasks']:
            patient=Patient.objects.get(pk=row['patient_id'])
            self.assertEqual((row['patient_revision'],row['load']),(patient.revision,patient.load))
            self.assertNotIn(patient.name,json.dumps(pending.context,ensure_ascii=False))
        self.assertEqual(pending.context['schedule_publication'],self.draft.schedule_publication_id)
        self.assertFalse(AllocationFeedback.objects.filter(business_type='BED_ASSIGNMENT',adopted=True).exists())
        # An intervening adjustment makes the first feedback stale.
        beds.modify(self.draft,self.manager,{**context(),'assignments':original,'reason':'患者病情变化'},record_adjustment=True)
        publication=beds.publish(self.draft,self.manager,{**context(),'confirm_three':True})
        adopted=list(AllocationFeedback.objects.filter(business_type='BED_ASSIGNMENT',adopted=True))
        self.assertEqual(len(adopted),1)
        self.assertEqual(adopted[0].before,alternative);self.assertEqual(adopted[0].after,original)
        self.assertEqual(adopted[0].task_reference,f'bed-publication:{publication.id}')
        last_pending=AllocationFeedback.objects.filter(business_type='BED_ASSIGNMENT',adopted=False).order_by('-id').first()
        self.assertEqual(adopted[0].context,last_pending.context)
        # Idempotent re-publication must not duplicate adopted feedback.
        beds.publish(self.draft,self.manager,{**context(),'confirm_three':True})
        self.assertEqual(AllocationFeedback.objects.filter(business_type='BED_ASSIGNMENT',adopted=True).count(),1)
        snapshot=copy.deepcopy(adopted[0].context)
        patient=Patient.objects.get(pk=snapshot['patient_tasks'][0]['patient_id'])
        patient.load='high' if patient.load!='high' else 'light';patient.save(update_fields=['load'])
        adopted[0].refresh_from_db()
        self.assertEqual(adopted[0].context,snapshot)


class AllocationConversationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.group=CareGroup.objects.create(name='合成对话组')
        cls.manager=get_user_model().objects.create_user('dialog-manager',password='synthetic')
        AccountProfile.objects.create(user=cls.manager,group=cls.group,role='group_manager')

    def setUp(self):
        self.client=APIClient();self.client.force_authenticate(self.manager)
        env=patch.dict(os.environ,{'AI_PERSONNEL_EXTERNAL_AUTHORIZED':'false','AI_HOSPITAL_AUTHORIZED':'false'})
        env.start();self.addCleanup(env.stop)
        self.config=patch('scheduling.api.allocation_assistant.public_config',return_value=CONFIG);self.config.start();self.addCleanup(self.config.stop)
        self.key=patch('scheduling.api.allocation_assistant.secret_value',return_value='synthetic-key');self.key.start();self.addCleanup(self.key.stop)
        self.model=patch('scheduling.api.allocation_assistant.chat_completion',return_value='仅依据当前汇总结果供人工核查。');self.calls=self.model.start();self.addCleanup(self.model.stop)
        def fake_stream(config,key,messages,snapshot):
            yield ('delta',self.calls(config,key,messages,scene_config=snapshot))
            yield ('done',{'request_id':'synthetic'})
        self.streaming=patch('scheduling.api.allocation_assistant.stream_completion',side_effect=fake_stream)
        self.streaming.start();self.addCleanup(self.streaming.stop)

    def post(self,kind,message,**kwargs):
        data={'group':self.group.pk,'kind':kind,'message':message,'synthetic':True,'consent':True,**kwargs}
        return self.client.post('/api/v1/allocation-assistant/',data,format='json')

    def test_multi_turn_context_persists_and_business_types_are_isolated(self):
        self.assertEqual(self.post('GROUP_ASSIGNMENT','合成分组问题一').status_code,200)
        self.assertEqual(self.post('GROUP_ASSIGNMENT','合成分组问题二').status_code,200)
        second=self.calls.call_args.args[2]
        self.assertEqual([m['role'] for m in second],['system','user','assistant','user'])
        self.assertEqual(second[1]['content'],'合成分组问题一')
        self.assertEqual(self.post('BED_ASSIGNMENT','合成床位问题一').status_code,200)
        bed_messages=self.calls.call_args.args[2]
        self.assertEqual([m['role'] for m in bed_messages],['system','user'])
        self.assertNotIn('合成分组问题一',str(bed_messages))
        self.assertEqual(AllocationConversation.objects.count(),2)
        response=self.client.get('/api/v1/allocation-assistant/',{'group':self.group.pk,'kind':'GROUP_ASSIGNMENT'})
        self.assertEqual(response.status_code,200);self.assertEqual(len(response.data['data']['messages']),4)

    def test_message_links_existing_task_and_rejects_other_business_or_scope(self):
        task=AllocationExecution.objects.create(group=self.group,created_by=self.manager,kind='GROUP_ASSIGNMENT',status='completed')
        result=self.post('GROUP_ASSIGNMENT','请解释任务',execution_id=task.id)
        self.assertEqual(result.status_code,200)
        self.assertTrue(all(m['execution_id']==task.id for m in result.data['data']['messages']))
        history=self.client.get('/api/v1/allocation-assistant/',{'group':self.group.id,'kind':'GROUP_ASSIGNMENT'}).data['data']
        self.assertEqual(history['messages'][-1]['execution_id'],task.id)
        self.assertEqual(self.post('BED_ASSIGNMENT','不能跨业务',execution_id=task.id).status_code,404)
        other=CareGroup.objects.create(name='其他合成组')
        foreign=AllocationExecution.objects.create(group=other,created_by=self.manager,kind='GROUP_ASSIGNMENT',status='completed')
        self.assertEqual(self.post('GROUP_ASSIGNMENT','不能跨大组',execution_id=foreign.id).status_code,404)

    def test_no_consent_or_no_personnel_authorization_prevents_model_call(self):
        self.assertEqual(self.post('GROUP_ASSIGNMENT','问题',consent=False).status_code,400)
        self.assertEqual(self.post('GROUP_ASSIGNMENT','问题',synthetic=False).status_code,403)
        self.calls.assert_not_called();self.assertFalse(AllocationConversation.objects.exists())

    def test_new_session_preserves_history_and_does_not_mix_model_context(self):
        self.assertEqual(self.post('GROUP_ASSIGNMENT','第一段对话').status_code,200)
        created=self.client.post('/api/v1/allocation-assistant/',{'group':self.group.pk,'kind':'GROUP_ASSIGNMENT','action':'new'},format='json')
        self.assertEqual(created.status_code,200)
        new_id=created.data['data']['session_id']
        self.assertEqual(created.data['data']['messages'],[])
        response=self.post('GROUP_ASSIGNMENT','第二段对话',session_id=new_id)
        self.assertEqual(response.status_code,200)
        self.assertNotIn('第一段对话',str(self.calls.call_args.args[2]))
        history=self.client.get('/api/v1/allocation-assistant/',{'group':self.group.pk,'kind':'GROUP_ASSIGNMENT','session_id':'legacy'})
        self.assertEqual(history.status_code,200)
        self.assertEqual(history.data['data']['messages'][0]['content'],'第一段对话')
        self.assertEqual(len(history.data['data']['sessions']),2)

    def test_selected_group_candidate_is_pseudonymised_before_model_call(self):
        from scheduling.models import AIGroupingTask
        from django.utils import timezone
        person={'id':901,'name':'合成姓名甲','employee_id':'SYN-901','level':'N2','years':5,
            'subgroup':1,'is_leader':True,'skills':['CRRT'],'certifications':[],
            'teaching_collaboration':'合成姓名甲负责带教','assessment_level':None,'special_restrictions':''}
        task=AIGroupingTask.objects.create(group=self.group,created_by=self.manager,effective_date=timezone.localdate(),
            participants=[person],fingerprint='a'*64,ai_model='synthetic-test-model',
            parameters={'weights':{'skill_coverage':.9}},
            plans=[{'code':'A','assignments':{'901':1},'leaders':{'1':901},'metrics':{'skill_coverage':100}}])
        self.calls.return_value='N001的技能资料需要人工核对。'
        response=self.post('GROUP_ASSIGNMENT','请说明合成姓名甲的分组',context={'task_id':task.id,'plan_code':'A'})
        self.assertEqual(response.status_code,200)
        sent=str(self.calls.call_args.args[2])
        self.assertNotIn('合成姓名甲',sent);self.assertNotIn('SYN-901',sent)
        self.assertIn('N001',sent);self.assertIn('CRRT',sent)
        self.assertIn('合成姓名甲',response.data['data']['messages'][-1]['content'])

    def test_real_patient_cannot_be_mislabeled_synthetic(self):
        from scheduling.models import Bed
        bed=Bed.objects.create(group=self.group,number=1)
        Patient.objects.create(group=self.group,bed=bed,code='REAL-TEST',name='测试',synthetic=False)
        self.assertEqual(self.post('BED_ASSIGNMENT','问题',synthetic=True).status_code,403)
        self.calls.assert_not_called()
