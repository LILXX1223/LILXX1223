"""Isolated regression tests for published-roster sync and solver progress."""
from datetime import date
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from scheduling.models import BedDraft, CareGroup, Nurse, Patient, ScheduleDraft, SchedulePublication
from scheduling.services import beds
from scheduling.services.common import BusinessError


DAY=date(2026,10,22)


class BedRuntimeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Django's test runner creates a separate test database for this fixture.
        with patch('django.utils.timezone.localdate',return_value=DAY):
            call_command('seed_demo',verbosity=0)
        cls.group=CareGroup.objects.get(name='第3组')
        cls.draft=BedDraft.objects.get(group=cls.group,date=DAY,shift='A')
        from django.contrib.auth import get_user_model
        cls.manager=get_user_model().objects.get(username='group3')

    def test_roster_comes_from_the_published_exact_shift_not_draft(self):
        pub,nurses=beds.on_duty(self.group,DAY,'A')
        expected={e['nurse'] for e in pub.snapshot['entries'] if e['date']==str(DAY) and e['shift']=='A'}
        self.assertEqual({n.id for n in nurses},expected)
        _,day_nurses=beds.on_duty(self.group,DAY,'DAY')
        self.assertTrue(day_nurses)
        self.assertFalse(expected & {n.id for n in day_nurses})
        schedule=ScheduleDraft.objects.get(group=self.group)
        for entry in schedule.entries:
            if entry['date']==str(DAY): entry['shift']='N'
        schedule.save(update_fields=['entries'])
        self.assertEqual({n.id for n in beds.on_duty(self.group,DAY,'A')[1]},expected)
        self.assertEqual(SchedulePublication.objects.count(),1)

    def test_roster_and_publication_use_same_snapshot(self):
        publication=SchedulePublication.objects.get(group=self.group)
        with patch('scheduling.services.beds.schedule.current_publication',return_value=publication) as lookup:
            actual,nurses=beds.on_duty(self.group,DAY,'A')
        self.assertEqual(actual.id,publication.id)
        self.assertTrue(nurses)
        lookup.assert_called_once_with(self.group,DAY)

    def test_no_current_eligible_nurses_fails_without_changing_roster(self):
        before=self.draft.roster[:]
        Nurse.objects.filter(group=self.group,id__in=before).update(status='paused')
        context={'revision':self.draft.revision,'fingerprint':beds.fingerprint(self.draft)}
        with self.assertRaises(BusinessError) as raised:
            beds.sync(self.draft,self.manager,context)
        self.assertIn('当前日期与班次未找到有效的正式当班护士',str(raised.exception))
        self.draft.refresh_from_db();self.assertEqual(self.draft.roster,before)

    def test_sync_reports_stale_responsibility_without_deleting_it(self):
        nurse_id=next(iter(self.draft.assignments.values()))
        Nurse.objects.filter(pk=nurse_id).update(status='paused')
        client=APIClient();client.force_authenticate(self.manager)
        context={'group':self.group.pk,'id':self.draft.pk,'revision':self.draft.revision,
                 'fingerprint':beds.fingerprint(self.draft)}
        response=client.post('/api/v1/beds/sync/',context,format='json')
        self.assertEqual(response.status_code,200)
        self.assertTrue(any(item['nurse_id']==nurse_id for item in response.data['data']['conflicts']))
        self.draft.refresh_from_db()
        self.assertIn(nurse_id,self.draft.assignments.values())
        self.assertFalse(beds.validate(self.draft)['valid'])

    def test_real_solver_events_preview_and_callback_order(self):
        events=[]; snapshots=[]; original=dict(self.draft.assignments)
        def progress(stage,status,summary): events.append((stage,status,summary))
        def before_solve(draft,nurses,patients):
            snapshots.append((len(nurses),len(patients)))
            self.assertEqual(events[-1][:2],(3,'completed'))
        result=beds.recommend(self.draft,progress=progress,before_solve=before_solve)
        self.assertTrue(snapshots)
        self.assertEqual([event[0] for event in events if event[1]=='completed'],[1,2,3,5,6,7])
        self.assertIn(result['solver']['status'],['optimal','feasible','timeout_feasible'])
        self.assertGreaterEqual(result['solver']['elapsed_seconds'],0)
        self.assertTrue(result['validation']['valid'])
        self.draft.refresh_from_db();self.assertEqual(self.draft.assignments,original)

    def test_ai_step_data_change_stops_before_solver(self):
        events=[]
        def change(draft,nurses,patients):
            Patient.objects.filter(pk=patients[0].pk).update(revision=patients[0].revision+1)
        with self.assertRaises(BusinessError) as raised:
            beds.recommend(self.draft,progress=lambda *event:events.append(event),before_solve=change)
        self.assertEqual(raised.exception.status_code,409)
        self.assertFalse(any(event[0]==5 for event in events))

    def test_infeasible_solver_does_not_claim_success(self):
        self.group.rules['max_beds']=2;self.group.save(update_fields=['rules']);self.draft.group=self.group
        events=[]
        with self.assertRaises(BusinessError):
            beds.recommend(self.draft,progress=lambda *event:events.append(event))
        solver=[e for e in events if e[0]==5 and e[1]=='failed']
        self.assertEqual(solver[0][2]['status'],'infeasible')
        self.assertFalse(any(e[0]==7 for e in events))
