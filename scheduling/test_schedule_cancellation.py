"""Cancellation overlays use isolated fixtures and preserve historical releases."""
import copy
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from scheduling.cancellation_models import ScheduleCancellation, ScheduleCancellationHold
from scheduling.models import BedPublication, CareGroup, Nurse, ScheduleDraft, SchedulePublication
from scheduling.services import beds, schedule, schedule_cancellation, workload
from scheduling.services.common import BusinessError

DAY=date(2026,10,22)


class ScheduleCancellationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with patch('django.utils.timezone.localdate',return_value=DAY):call_command('seed_demo',verbosity=0)
        cls.group=CareGroup.objects.get(name='第3组')
        cls.manager=get_user_model().objects.get(username='group3')
        cls.head=get_user_model().objects.get(username='head_nurse')

    def setUp(self):
        self.publication=SchedulePublication.objects.get(group=self.group)
        self.draft=ScheduleDraft.objects.get(group=self.group)
        self.entry=next(e for e in self.publication.snapshot['entries'] if e['date']==str(DAY) and e['shift']=='A')
        self.data={'group':self.group.id,'start_date':str(DAY),'end_date':str(DAY),'nurse_scope':'all',
                   'nurse_ids':[self.entry['nurse']],'shift':'A','reason':'临时管理需要','historical_correction':True}
        self.client=APIClient();self.client.force_authenticate(self.manager)

    def test_preview_cancel_preserves_history_and_updates_roster(self):
        original=copy.deepcopy(self.publication.snapshot)
        bed_snapshot=copy.deepcopy(BedPublication.objects.first().snapshot)
        previous_workload=workload.summary(self.group,DAY,nurse_id=self.entry['nurse'])['rows'][0]
        before=schedule_cancellation.preview(self.group,self.data)
        self.assertEqual(before['affected_cells'],1);self.assertEqual(before['published_records'],1)
        result=schedule_cancellation.cancel(self.group,self.manager,{**self.data,'fingerprint':before['fingerprint'],'confirmed':True})
        self.assertEqual(result['affected_cells'],1)
        self.publication.refresh_from_db();self.assertEqual(self.publication.snapshot,original)
        self.assertEqual(BedPublication.objects.first().snapshot,bed_snapshot)
        self.assertFalse(any(e['nurse']==self.entry['nurse'] for e in schedule.published_entries(self.group,DAY)))
        self.assertNotIn(self.entry['nurse'],[n.id for n in beds.on_duty(self.group,DAY,'A')[1]])
        current_workload=workload.summary(self.group,DAY,nurse_id=self.entry['nurse'])['rows'][0]
        self.assertEqual(current_workload['total_shifts'],previous_workload['total_shifts']-1)
        self.assertEqual(current_workload['patients'],previous_workload['patients'])
        self.draft.refresh_from_db()
        self.assertFalse(any(e['nurse']==self.entry['nurse'] and e['date']==str(DAY) for e in self.draft.entries))
        self.assertEqual(ScheduleCancellation.objects.count(),1)

    def test_explicit_new_generation_is_not_hidden_by_earlier_hold(self):
        preview=schedule_cancellation.preview(self.group,self.data)
        schedule_cancellation.cancel(self.group,self.manager,{**self.data,'fingerprint':preview['fingerprint'],'confirmed':True})
        self.draft.refresh_from_db()
        regenerated=schedule.generate(self.group,self.manager,self.draft.start_date,revision=self.draft.revision)
        self.assertTrue(any(e['nurse']==self.entry['nurse'] and e['date']==str(DAY) for e in regenerated.entries))
        self.assertTrue(ScheduleCancellationHold.objects.get(group=self.group,nurse_id_at_action=self.entry['nurse'],date=DAY).active)
        changed=schedule.edit(regenerated,self.manager,{'nurse':self.entry['nurse'],'date':str(DAY),'shift':'A',
            'reason':'负责人重新安排','revision':regenerated.revision})
        self.assertTrue(any(e['nurse']==self.entry['nurse'] and e['date']==str(DAY) for e in changed.entries))
        self.assertFalse(ScheduleCancellationHold.objects.get(group=self.group,nurse_id_at_action=self.entry['nurse'],date=DAY).active)
        # Manual drafting does not rewrite the earlier publication.
        self.assertFalse(any(e['nurse']==self.entry['nurse'] for e in schedule.published_entries(self.group,DAY)))

    def test_stale_preview_and_missing_confirmation_do_not_change_data(self):
        preview=schedule_cancellation.preview(self.group,self.data)
        self.draft.revision+=1;self.draft.save()
        with self.assertRaises(BusinessError) as error:
            schedule_cancellation.cancel(self.group,self.manager,{**self.data,'fingerprint':preview['fingerprint'],'confirmed':True})
        self.assertEqual(error.exception.status_code,409)
        with self.assertRaises(BusinessError):schedule_cancellation.cancel(self.group,self.manager,self.data)
        self.assertFalse(ScheduleCancellation.objects.exists())

    def test_date_range_shift_and_permission_checks(self):
        before=self.client.post('/api/v1/schedules/cancel-preview/',self.data,format='json')
        self.assertEqual(before.status_code,200);self.assertEqual(before.data['data']['affected_cells'],1)
        invalid={**self.data,'start_date':str(DAY+timedelta(days=1))}
        self.assertEqual(self.client.post('/api/v1/schedules/cancel-preview/',invalid,format='json').status_code,400)
        self.client.force_authenticate(self.head)
        self.assertEqual(self.client.post('/api/v1/schedules/cancel/',{**self.data,'confirmed':True},format='json').status_code,403)
        self.assertFalse(ScheduleCancellation.objects.exists())

    def test_draft_only_cancellation_does_not_change_formal_roster(self):
        entry=next(e for e in self.draft.entries if e['nurse']==self.entry['nurse'] and e['date']==str(DAY))
        entry['shift']='DAY';self.draft.save()
        data={**self.data,'shift':'DAY'};preview=schedule_cancellation.preview(self.group,data)
        self.assertEqual(preview['published_records'],0);self.assertEqual(preview['draft_records'],1)
        schedule_cancellation.cancel(self.group,self.manager,{**data,'fingerprint':preview['fingerprint'],'confirmed':True})
        self.assertTrue(any(e['nurse']==self.entry['nurse'] for e in schedule.published_entries(self.group,DAY)))

    def test_cancelled_approved_request_no_longer_claims_to_be_published(self):
        from scheduling.models import ShiftRequest
        from scheduling.api.core import request_data
        nurse=Nurse.objects.get(pk=self.entry['nurse'])
        request=ShiftRequest.objects.create(group=self.group,nurse=nurse,date=DAY,kind='change',target_shift='A',
            reason='合成测试申请',decision='approved',submitted_by=self.manager)
        snapshot=copy.deepcopy(self.publication.snapshot)
        next(e for e in snapshot['entries'] if e['date']==str(DAY) and e['nurse']==nurse.id)['request_id']=request.id
        SchedulePublication.objects.create(group=self.group,draft=self.draft,draft_revision=self.publication.draft_revision+100,
            start_date=self.publication.start_date,snapshot=snapshot,published_by=self.manager)
        self.assertEqual(request_data(request)['status'],'published')
        report=schedule_cancellation.preview(self.group,self.data)
        self.assertEqual(report['related_requests'],1)
        schedule_cancellation.cancel(self.group,self.manager,{**self.data,'fingerprint':report['fingerprint'],'confirmed':True})
        self.assertEqual(request_data(request)['status'],'approved')
        self.assertTrue(ShiftRequest.objects.filter(pk=request.id).exists())
