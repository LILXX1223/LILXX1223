from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from scheduling.models import AccountProfile, Bed, CareGroup, Nurse, NurseSkill, Patient, ScheduleDraft, SchedulePublication, ShiftType, Skill


class OverviewTests(TestCase):
    def setUp(self):
        self.group=CareGroup.objects.create(name='总览合成组')
        self.user=get_user_model().objects.create_user('overview-user',password='test-password')
        AccountProfile.objects.create(user=self.user,role='group_manager',group=self.group)
        self.nurse=Nurse.objects.create(group=self.group,name='合成护士',employee_id='SYN-OVERVIEW',level='N2',qualifications=['A'])
        bed=Bed.objects.create(group=self.group,number=1)
        Patient.objects.create(group=self.group,bed=bed,name='合成患者',code='SYN-OVERVIEW-P',synthetic=True)
        skill=Skill.objects.create(group=self.group,code='CRRT',name='CRRT')
        NurseSkill.objects.create(nurse=self.nurse,skill=skill,independent=False)
        self.client=APIClient();self.client.force_authenticate(self.user)

    def test_missing_event_and_staffing_sources_are_not_zero_or_fake(self):
        result=self.client.get('/api/v1/overview/',{'group':self.group.id}).data['data']
        self.assertEqual((result['beds'],result['patients']),(1,1));self.assertIsNone(result['admissions']);self.assertIsNone(result['discharges'])
        self.assertIsNone(result['publication']);self.assertTrue(all(row['count'] is None and row['coverage'] is None for row in result['shifts']))
        self.assertEqual(result['levels']['N2'],1);self.assertEqual(result['skills'][0]['independent'],0)

    def test_current_published_roster_uses_saved_target_with_confirmation_flag(self):
        day=timezone.localdate();start=day-timedelta(days=day.weekday())
        ShiftType.objects.create(group=self.group,code='A',name='A班',effective_from=day)
        draft=ScheduleDraft.objects.create(group=self.group,start_date=start,entries=[])
        SchedulePublication.objects.create(group=self.group,draft=draft,draft_revision=1,start_date=start,published_by=self.user,
            snapshot={'entries':[{'nurse':self.nurse.id,'date':str(day),'shift':'A','subgroup':1,'base_type':'rotation'}]})
        result=self.client.get('/api/v1/overview/',{'group':self.group.id}).data['data']
        shift=next(row for row in result['shifts'] if row['code']=='A')
        self.assertEqual(shift['count'],1);self.assertEqual(shift['need'],3);self.assertEqual(shift['coverage'],33.3)
        self.assertFalse(result['requirements_confirmed'])

    def test_deleted_or_inactive_snapshot_staff_are_not_counted_as_available(self):
        day=timezone.localdate();start=day-timedelta(days=day.weekday())
        ShiftType.objects.create(group=self.group,code='A',name='A班',effective_from=day)
        draft=ScheduleDraft.objects.create(group=self.group,start_date=start,entries=[])
        SchedulePublication.objects.create(group=self.group,draft=draft,draft_revision=1,start_date=start,published_by=self.user,
            snapshot={'entries':[{'nurse':self.nurse.id,'date':str(day),'shift':'A'},
                                 {'nurse':987654,'date':str(day),'shift':'A'}]})
        result=self.client.get('/api/v1/overview/',{'group':self.group.id}).data['data']
        self.assertEqual(result['unavailable_roster_count'],1)
        self.assertEqual(next(row for row in result['shifts'] if row['code']=='A')['count'],1)
        self.assertTrue(any(item['title']=='正式排班含当前不可用人员' for item in result['alerts']))
