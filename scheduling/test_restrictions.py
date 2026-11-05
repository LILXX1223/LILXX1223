from datetime import date
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient
from scheduling.models import CareGroup, Nurse, ScheduleDraft, SchedulePublication
from scheduling.services import schedule


class ScheduleRestrictionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with patch('django.utils.timezone.localdate',return_value=date(2026,10,22)):
            call_command('seed_demo',verbosity=0)
        cls.group=CareGroup.objects.get(name='第3组')
        cls.manager=get_user_model().objects.get(username='group3')

    def test_restriction_is_saved_invalidates_draft_and_requires_human_review(self):
        nurse=Nurse.objects.first();draft=ScheduleDraft.objects.get();old=draft.revision
        client=APIClient();client.force_authenticate(self.manager)
        response=client.patch(f'/api/v1/nurses/{nurse.id}/',{'group':self.group.id,'revision':nurse.revision,
            'special_restrictions':'需管理者确认特殊安排'},format='json')
        self.assertEqual(response.status_code,200);draft.refresh_from_db();self.assertEqual(draft.revision,old+1)
        report=schedule.validate(draft)
        self.assertTrue(any('需管理者确认特殊安排' in x['message'] for x in report['warnings']))
        self.assertEqual(SchedulePublication.objects.count(),1)
