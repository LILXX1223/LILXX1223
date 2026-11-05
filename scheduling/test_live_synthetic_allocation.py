"""Opt-in live model integration against an isolated synthetic test database.

Set ICU_LIVE_SYNTHETIC_AI=1 and supply in-process ICU_TEST_AI_* values; no
credentials are stored in this file. Normal test runs skip these external calls.
"""
import json
import os
from datetime import date
from unittest import skipUnless
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from scheduling.models import CareGroup, BedDraft
from scheduling.services import allocation_execution, beds

DAY=date(2026,10,22)


@skipUnless(os.environ.get('ICU_LIVE_SYNTHETIC_AI')=='1','requires explicit synthetic live AI test')
class LiveSyntheticAllocationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with patch('django.utils.timezone.localdate',return_value=DAY):call_command('seed_demo',verbosity=0)
        cls.group=CareGroup.objects.get(name='第3组');cls.user=get_user_model().objects.get(username='group3')
        cls.config=json.loads(os.environ['ICU_TEST_AI_CONFIG'])
        cls.scenes=json.loads(os.environ['ICU_TEST_AI_SCENES'])
        cls.key=os.environ['ICU_TEST_AI_KEY']

    def setUp(self):
        self.day=patch('django.utils.timezone.localdate',return_value=DAY);self.day.start();self.addCleanup(self.day.stop)
        patches=[patch('scheduling.services.ai_configuration.public_config',return_value=self.config),
                 patch('scheduling.services.grouping.public_config',return_value=self.config),
                 patch('scheduling.services.ai_scenes.scene_snapshot',side_effect=lambda scene,*args:self.scenes[scene])]
        for p in patches:p.start();self.addCleanup(p.stop)

    def test_real_model_and_group_solver(self):
        args={'group_count':4,'group_sizes':[3,3,3,3],'min_leaders':1,'junior_years':3,
              'mentor_ids':[],'locked':{},'effective_date':str(DAY),'synthetic':True,'consent':True}
        task=allocation_execution.start(self.group,self.user,'GROUP_ASSIGNMENT',args,dispatch=False)
        allocation_execution.run(task.id,api_key=self.key);task.refresh_from_db()
        self.assertEqual(task.status,'completed',task.error)
        self.assertEqual(list(task.events.filter(status='completed').values_list('stage',flat=True)),[1,2,3,4,5,6,7])
        self.assertTrue(task.result.get('grouping_task_id'))

    def test_real_model_and_bed_solver(self):
        draft=BedDraft.objects.get(group=self.group,date=DAY,shift='A')
        args={'id':draft.id,'revision':draft.revision,'fingerprint':beds.fingerprint(draft),'synthetic':True,'consent':True}
        task=allocation_execution.start(self.group,self.user,'BED_ASSIGNMENT',args,dispatch=False)
        allocation_execution.run(task.id,api_key=self.key);task.refresh_from_db()
        self.assertEqual(task.status,'completed',task.error)
        self.assertEqual(list(task.events.filter(status='completed').values_list('stage',flat=True)),[1,2,3,4,5,6,7])
        self.assertTrue(task.result['validation']['valid'])
