"""Isolated synthetic tests; model responses below are test doubles, not live AI."""
import copy
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch, Mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from scheduling.experience_models import AllocationExperience
from scheduling.models import AIGroupingTask, BedDraft, BedPublication, CareGroup, Nurse, Patient, Extraction
from scheduling.services import allocation_execution, beds, experiences
from scheduling.services.common import BusinessError
from scheduling.task_models import AllocationConversation, AllocationExecution

DAY=date(2026,10,22)
CONFIG={'configured':True,'provider':'deepseek','base_url':'https://api.deepseek.com',
        'model':'synthetic-test-model','hospital_authorized':False}


class AllocationRoundTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with patch('django.utils.timezone.localdate',return_value=DAY):call_command('seed_demo',verbosity=0)
        cls.group=CareGroup.objects.get(name='第3组')
        cls.manager=get_user_model().objects.get(username='group3')
        cls.draft=BedDraft.objects.get(group=cls.group,date=DAY,shift='A')

    def setUp(self):
        self.day=patch('django.utils.timezone.localdate',return_value=DAY);self.day.start();self.addCleanup(self.day.stop)
        self.client=APIClient();self.client.force_authenticate(self.manager)
        self.group_args={'kind':'GROUP_ASSIGNMENT','group_count':4,'group_sizes':[3,3,3,3],
            'min_leaders':1,'junior_years':3,'mentor_ids':[],'locked':{},'effective_date':str(DAY),'synthetic':True,'consent':True}
        self.bed_args={'kind':'BED_ASSIGNMENT','id':self.draft.id,'revision':self.draft.revision,
            'fingerprint':beds.fingerprint(self.draft),'synthetic':True,'consent':True}

    @staticmethod
    def group_model(config,key,messages,**kwargs):
        data=json.loads(messages[1]['content']);people=data['nurses']
        return json.dumps({'analyses':[{'code':p['code'],'summary':'合成资料'} for p in people],
            'candidate_groups':{p['code']:i%data['group_count']+1 for i,p in enumerate(people)},
            'complementarity':[],'teaching':[],'concerns':[],
            'optimization_suggestions':[{'title':'均衡技能','description':'合成测试建议','target':'skill_coverage','weight':0.4}]})

    def run_group(self,task):
        with patch('scheduling.services.grouping.public_config',return_value=CONFIG), \
             patch('scheduling.services.grouping.secret_value',return_value='synthetic-key'), \
             patch('scheduling.services.grouping.chat_completion',side_effect=self.group_model):
            allocation_execution.run(task.id)
        task.refresh_from_db();self.assertEqual(task.status,'completed',task.error)
        return task

    def run_bed(self,task):
        with patch('scheduling.services.ai_configuration.public_config',return_value=CONFIG), \
             patch('scheduling.services.ai_configuration.secret_value',return_value='synthetic-key'), \
             patch('scheduling.services.ai_configuration.chat_completion',return_value=json.dumps({'summary':'合成核对','optimization_suggestions':[]})):
            allocation_execution.run(task.id)
        task.refresh_from_db();self.assertEqual(task.status,'completed',task.error)
        return task

    def test_assessment_need_is_independent_and_high_rank_is_preferred(self):
        _,nurses=beds.on_duty(self.group,DAY,'A')
        for i,nurse in enumerate(nurses):
            nurse.assessment_level=(i%3)+1;nurse.save(update_fields=['assessment_level'])
        patient=Patient.objects.filter(group=self.group,bed__isnull=False).first()
        # The test patient has explicit human-confirmed need, not an inferred load.
        patient.assessment_need='higher';patient.save(update_fields=['assessment_need'])
        experience=experiences.create(self.group,self.manager,{'business_type':experiences.BED,
            'title':'评估匹配','description':'优先考虑已确认的较高评估需求','target':'assessment_match','weight':1.0})
        experiences.change_status(self.group,self.manager,experience,{'action':'adopt','revision':experience.revision,'confirmed':True})
        recorded=[];original=beds.cp_model.CpSolver.Solve
        def inspect(solver,model):
            proto=model.Proto();recorded.append({proto.variables[i].name:c for i,c in zip(proto.objective.vars,proto.objective.coeffs)})
            return original(solver,model)
        with patch.object(beds.cp_model.CpSolver,'Solve',inspect):result=beds.recommend(self.draft)
        self.assertEqual(recorded[-1]['normalized_assessment_match'],10)
        self.assertTrue(result['validation']['valid']);self.assertEqual(result['evaluation']['assessment_match']['comparable_count'],1)
        match=result['evaluation']['assessment_match']['matches'][0]
        self.assertIn(match['assessment_level'],(1,2,3));self.assertEqual(match['bed_id'],patient.bed_id)
        # A missing qualified rank disables comparison for this patient rather
        # than silently treating the unknown value as best or worst.
        eligible=next(n for n in nurses if beds.qualified(n,patient,DAY,self.group.rules))
        eligible.assessment_level=None;eligible.save(update_fields=['assessment_level'])
        result=beds.recommend(self.draft)
        self.assertEqual(result['evaluation']['assessment_match']['comparable_count'],0)
        self.assertEqual(result['effective_weights']['assessment_match'],0)
        self.assertTrue(any(e['id']==experience.id for e in result['excluded_experiences']))

    def test_solver_uses_ordinal_direction_without_equating_load_to_need(self):
        patient=SimpleNamespace(bed_id=1,load='light',assessment_need='higher',reviewed_by_id=1)
        nurses=[SimpleNamespace(id=1,assessment_level=1,years=10),SimpleNamespace(id=2,assessment_level=3,years=10)]
        draft=SimpleNamespace(group=self.group,date=DAY,shift='A',revision=1,assignments={'1':2},
                              refresh_from_db=Mock())
        preference={'weights':{'load_balance':0.0,'minimize_three':0.0,'continuity':0.1,'assessment_match':0.0},'applied':[],'excluded':[]}
        with patch.object(beds,'on_duty',return_value=(SimpleNamespace(id=1),nurses)), \
             patch.object(beds,'patients',return_value=[patient]), \
             patch.object(beds,'qualified',return_value=True), \
             patch.object(beds,'fingerprint',return_value='same'), \
             patch.object(beds,'validate',return_value={'valid':True,'errors':[],'warnings':[],'fingerprint':'same'}):
            before=beds.recommend(draft,preference_snapshot=copy.deepcopy(preference))
            preference['weights']['assessment_match']=1.0
            after=beds.recommend(draft,preference_snapshot=copy.deepcopy(preference))
            self.assertEqual(before['assignments']['1'],2)
            self.assertEqual(after['assignments']['1'],1)
            self.assertEqual(after['evaluation']['assessment_match']['mean_ordinal_level'],1)
            # No human-confirmed need: even a high-load patient is not a match target.
            patient.load='high';patient.assessment_need=''
            no_need=beds.recommend(draft,preference_snapshot=copy.deepcopy(preference))
            self.assertEqual(no_need['evaluation']['assessment_match']['higher_need_count'],0)
            self.assertEqual(no_need['effective_weights']['assessment_match'],0)

    def test_model_extraction_cannot_set_assessment_need_but_human_edit_can(self):
        from scheduling.services.bed_configuration import public_number
        patient=Patient.objects.filter(group=self.group,bed__isnull=False).first()
        number=public_number(patient.bed.number)
        extraction=Extraction.objects.create(group=self.group,created_by=self.manager,mode='external',result=[{
            'bed_number':number,'bed_id':patient.bed_id,'patient_id':patient.id,'patient_revision':patient.revision,'fields':{}}])
        fields={'name':patient.name,'load':patient.load,'synthetic':True,'assessment_need':'higher'}
        response=self.client.post('/api/v1/beds/review/',{'group':self.group.id,'extraction':extraction.id,
            'records':[{'bed_number':number,'fields':fields}],'confirmed':True},format='json')
        self.assertEqual(response.status_code,200);patient.refresh_from_db();self.assertEqual(patient.assessment_need,'')
        response=self.client.post('/api/v1/beds/patient/',{'group':self.group.id,'bed_id':patient.bed_id,
            'patient_id':patient.id,'patient_revision':patient.revision,'fields':fields,'confirmed':True},format='json')
        self.assertEqual(response.status_code,200);patient.refresh_from_db();self.assertEqual(patient.assessment_need,'higher')
        self.assertTrue(patient.versions.filter(snapshot__assessment_need='higher').exists())

    def test_three_group_rounds_preserve_snapshots_and_compare(self):
        before=list(Nurse.objects.filter(group=self.group).order_by('id').values_list('id','subgroup'))
        a=self.run_group(allocation_execution.start(self.group,self.manager,'GROUP_ASSIGNMENT',self.group_args,dispatch=False))
        pending=AllocationExperience.objects.get(pk=a.events.get(stage=4,status='completed').summary['pending_suggestions'][0]['id'])
        self.assertEqual(pending.status,'pending')
        b=self.run_group(allocation_execution.start(self.group,self.manager,'GROUP_ASSIGNMENT',
            {**self.group_args,'previous_task_id':a.id,'one_time_experience_ids':[pending.id]},dispatch=False))
        self.assertEqual(b.parameters['optimization_snapshot']['weights']['skill_coverage'],0.4)
        self.assertEqual(b.events.get(stage=5,status='completed').summary['plans'][0]['normalization']['skill_coverage']['coefficient'],4)
        experiences.change_status(self.group,self.manager,pending,{'action':'adopt','revision':pending.revision,'confirmed':True})
        c=self.run_group(allocation_execution.start(self.group,self.manager,'GROUP_ASSIGNMENT',
            {**self.group_args,'previous_task_id':b.id},dispatch=False))
        self.assertEqual(c.parameters['optimization_snapshot']['weights']['skill_coverage'],0.4)
        comparison=allocation_execution.compare_tasks(a,c)
        self.assertEqual([r['task_id'] for r in comparison['rounds']],[a.id,c.id])
        self.assertEqual(list(Nurse.objects.filter(group=self.group).order_by('id').values_list('id','subgroup')),before)
        response=self.client.get('/api/v1/allocation-tasks/compare/',{'group':self.group.id,'left':a.id,'right':c.id})
        self.assertEqual(response.status_code,200)

    def test_bed_round_snapshots_ignore_later_preference_changes(self):
        exp=experiences.create(self.group,self.manager,{'business_type':experiences.BED,'title':'负荷偏好',
            'description':'合成测试','target':'load_balance','weight':0.2})
        experiences.change_status(self.group,self.manager,exp,{'action':'adopt','revision':exp.revision,'confirmed':True})
        a=allocation_execution.start(self.group,self.manager,'BED_ASSIGNMENT',self.bed_args,dispatch=False)
        experiences.change_status(self.group,self.manager,exp,{'action':'pause','revision':exp.revision,'confirmed':True})
        a=self.run_bed(a)
        self.assertEqual(a.result['effective_weights']['load_balance'],0.2)
        b=self.run_bed(allocation_execution.start(self.group,self.manager,'BED_ASSIGNMENT',
            {**self.bed_args,'previous_task_id':a.id},dispatch=False))
        self.assertEqual(b.result['effective_weights']['load_balance'],1.0)
        self.assertEqual(len(allocation_execution.compare_tasks(a,b)['rounds']),2)
        self.assertEqual(BedPublication.objects.count(),1)
        self.draft.refresh_from_db();self.assertEqual(self.draft.revision,self.bed_args['revision'])

    def test_bed_model_suggestion_requires_confirmation_and_new_round(self):
        a=allocation_execution.start(self.group,self.manager,'BED_ASSIGNMENT',self.bed_args,dispatch=False)
        output={'summary':'合成分析','optimization_suggestions':[{'title':'均衡负荷','description':'合成偏好',
            'target':'load_balance','weight':0.3}]}
        with patch('scheduling.services.ai_configuration.public_config',return_value=CONFIG), \
             patch('scheduling.services.ai_configuration.secret_value',return_value='synthetic-key'), \
             patch('scheduling.services.ai_configuration.chat_completion',return_value=json.dumps(output)):
            allocation_execution.run(a.id)
        a.refresh_from_db();self.assertEqual(a.status,'completed',a.error)
        suggestion=a.events.get(stage=4,status='completed').summary['pending_suggestions'][0]
        self.assertEqual(a.result['effective_weights']['load_balance'],1.0)
        self.assertEqual(AllocationExperience.objects.get(pk=suggestion['id']).status,'pending')
        b=self.run_bed(allocation_execution.start(self.group,self.manager,'BED_ASSIGNMENT',
            {**self.bed_args,'previous_task_id':a.id,'experience_ids':[suggestion['id']]},dispatch=False))
        self.assertEqual(b.result['effective_weights']['load_balance'],0.3)
        self.assertEqual(b.result['solver']['coefficients']['load_balance'],3)
        experiences.change_status(self.group,self.manager,AllocationExperience.objects.get(pk=suggestion['id']),
            {'action':'adopt','revision':1,'confirmed':True})
        c=self.run_bed(allocation_execution.start(self.group,self.manager,'BED_ASSIGNMENT',
            {**self.bed_args,'previous_task_id':b.id},dispatch=False))
        self.assertEqual(c.result['effective_weights']['load_balance'],0.3)
        self.assertEqual(len({a.id,b.id,c.id}),3)
        self.assertEqual(BedPublication.objects.count(),1)

    def test_suggestion_records_verified_conversation_source_and_data_origin(self):
        msg_id='a'*32
        AllocationConversation.objects.create(group=self.group,kind=experiences.GROUP,messages=[
            {'id':msg_id,'role':'assistant','session_id':'legacy','content':'合成建议',
             'external_content':'合成建议','synthetic':True,'execution_id':None}])
        payload={'business_type':experiences.GROUP,'text':'合成建议','source_message_id':msg_id,
                 'synthetic':True,'consent':True}
        result=json.dumps({'title':'技能均衡','description':'合成建议','target':'skill_coverage','weight':0.7})
        with patch('scheduling.services.experiences.public_config',return_value=CONFIG), \
             patch('scheduling.services.experiences.secret_value',return_value='synthetic-key'), \
             patch('scheduling.services.experiences.chat_completion',return_value=result):
            item=experiences.suggest(self.group,self.manager,payload)
            self.assertEqual(item.source_details['source']['message_id'],msg_id)
            self.assertEqual(item.status,'pending')
            with self.assertRaises(BusinessError):experiences.suggest(self.group,self.manager,{**payload,'source_message_id':'b'*32})
            conversation=AllocationConversation.objects.get(group=self.group,kind=experiences.GROUP)
            conversation.messages[0]['synthetic']=False;conversation.save(update_fields=['messages'])
            with self.assertRaises(BusinessError):experiences.suggest(self.group,self.manager,payload)

    def test_stale_one_time_confirmation_cannot_use_edited_experience(self):
        exp=experiences.create(self.group,self.manager,{'business_type':experiences.GROUP,'title':'旧建议',
            'description':'合成测试','target':'skill_coverage','weight':0.2})
        old_revision=exp.revision
        experiences.change_status(self.group,self.manager,exp,{'action':'edit','revision':old_revision,'confirmed':True,
            'title':'修改建议','description':'合成测试','target':'skill_coverage','weight':0.7})
        with self.assertRaises(BusinessError) as error:
            allocation_execution.start(self.group,self.manager,'GROUP_ASSIGNMENT',
                {**self.group_args,'one_time_experience_ids':[exp.id],
                 'one_time_experience_revisions':{str(exp.id):old_revision}},dispatch=False)
        self.assertEqual(error.exception.status_code,409)
        self.assertFalse(AllocationExecution.objects.exists())

    def test_explicit_slider_precedes_long_term_experience(self):
        exp=experiences.create(self.group,self.manager,{'business_type':experiences.GROUP,'title':'长期技能',
            'description':'合成测试','target':'skill_coverage','weight':0.2})
        experiences.change_status(self.group,self.manager,exp,{'action':'adopt','revision':exp.revision,'confirmed':True})
        from scheduling.services.grouping import DEFAULT_WEIGHTS
        task=allocation_execution.start(self.group,self.manager,'GROUP_ASSIGNMENT',
            {**self.group_args,'weights':dict(DEFAULT_WEIGHTS),'explicit_weight_targets':['skill_coverage']},dispatch=False)
        self.assertEqual(task.parameters['optimization_snapshot']['weights']['skill_coverage'],0.9)
        self.assertIn('本次明确',task.parameters['optimization_snapshot']['excluded_experiences'][0]['reason'])

    def test_group_snapshot_remains_fixed_if_long_term_preference_changes(self):
        exp=experiences.create(self.group,self.manager,{'business_type':experiences.GROUP,'title':'长期技能',
            'description':'合成测试','target':'skill_coverage','weight':0.2})
        experiences.change_status(self.group,self.manager,exp,{'action':'adopt','revision':exp.revision,'confirmed':True})
        a=allocation_execution.start(self.group,self.manager,'GROUP_ASSIGNMENT',self.group_args,dispatch=False)
        experiences.change_status(self.group,self.manager,exp,{'action':'pause','revision':exp.revision,'confirmed':True})
        self.run_group(a)
        self.assertEqual(a.events.get(stage=3,status='completed').summary['weights']['skill_coverage'],0.2)
        grouping=AIGroupingTask.objects.get(pk=a.result['grouping_task_id'])
        self.assertEqual(grouping.plans[0]['normalization']['skill_coverage']['coefficient'],2)
        from scheduling.services.grouping import ensure_fresh
        ensure_fresh(grouping)
        b=self.run_group(allocation_execution.start(self.group,self.manager,'GROUP_ASSIGNMENT',self.group_args,dispatch=False))
        self.assertEqual(b.parameters['optimization_snapshot']['weights']['skill_coverage'],0.9)

    def test_superseded_worker_cannot_save_grouping_or_suggestions(self):
        a=allocation_execution.start(self.group,self.manager,'GROUP_ASSIGNMENT',self.group_args,dispatch=False)
        count=AIGroupingTask.objects.count(); created=[]
        def supersede_during_model(config,key,messages,**kwargs):
            b=allocation_execution.start(self.group,self.manager,'GROUP_ASSIGNMENT',
                {**self.group_args,'previous_task_id':a.id,'supersede_confirmed':True},dispatch=False)
            created.append(b.id)
            return self.group_model(config,key,messages,**kwargs)
        with patch('scheduling.services.grouping.public_config',return_value=CONFIG), \
             patch('scheduling.services.grouping.secret_value',return_value='synthetic-key'), \
             patch('scheduling.services.grouping.chat_completion',side_effect=supersede_during_model):
            allocation_execution.run(a.id)
        a.refresh_from_db();self.assertEqual(a.status,'superseded');self.assertEqual(a.result,{})
        self.assertEqual(AIGroupingTask.objects.count(),count)
        self.assertFalse(AllocationExperience.objects.filter(group=self.group,source='ai').exists())
        self.assertEqual(AllocationExecution.objects.get(pk=created[0]).status,'queued')

    def test_running_old_task_is_superseded_and_cannot_write_result(self):
        a=allocation_execution.start(self.group,self.manager,'GROUP_ASSIGNMENT',self.group_args,dispatch=False)
        AllocationExecution.objects.filter(pk=a.pk).update(status='running')
        with self.assertRaises(BusinessError):
            allocation_execution.start(self.group,self.manager,'GROUP_ASSIGNMENT',
                {**self.group_args,'previous_task_id':a.id},dispatch=False)
        b=allocation_execution.start(self.group,self.manager,'GROUP_ASSIGNMENT',
            {**self.group_args,'previous_task_id':a.id,'supersede_confirmed':True},dispatch=False)
        a.refresh_from_db();self.assertEqual(a.status,'superseded')
        with self.assertRaises(BusinessError):allocation_execution.event(a,5,'completed',{})
        allocation_execution.run(a.id);a.refresh_from_db();self.assertEqual(a.result,{})
        self.assertEqual(b.parameters['previous_task_id'],a.id)
