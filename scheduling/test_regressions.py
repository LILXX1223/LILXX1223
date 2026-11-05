"""Regressions for API validation and stale clinical/allocation input."""
import os
from datetime import date
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db.models import F
from django.test import TestCase, SimpleTestCase
from rest_framework.test import APIClient

from scheduling.models import AccountProfile, BedDraft, BedPublication, CareGroup, Nurse, Patient, ShiftRequest, ShiftType, ScheduleDraft, SchedulePublication
from scheduling.services import ai, beds
from scheduling.services.common import BusinessError


DAY = date(2026, 10, 22)


class ApiRegressionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with patch('django.utils.timezone.localdate', return_value=DAY):
            call_command('seed_demo', verbosity=0)
        cls.group = CareGroup.objects.get(name='第3组')
        cls.manager = get_user_model().objects.get(username='group3')
        cls.personal = get_user_model().objects.create_user('legacy-nurse',password='test-password',is_active=False)
        AccountProfile.objects.create(user=cls.personal,role='disabled_nurse',group=cls.group,nurse=Nurse.objects.first())

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.manager)
        self.mock_mode = patch.dict(os.environ, {'ICU_AI_ALLOW_MOCK':'true'})
        self.mock_mode.start()
        self.addCleanup(self.mock_mode.stop)

    def post(self, path, data):
        return self.client.post('/api/v1/' + path + '/', {'group': self.group.pk, **data}, format='json')

    def patient_data(self):
        patient = Patient.objects.first()
        return {'bed_id': patient.bed_id, 'patient_id': patient.pk, 'patient_revision': patient.revision,
                'confirmed': True, 'fields': {'name': patient.name, 'load': patient.load, 'synthetic': True}}

    def test_incomplete_shift_returns_validation_error(self):
        response = self.post('shifts', {'code': 'NEW', 'name': '新班次', 'working': True})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ShiftType.objects.filter(code='NEW').exists())

    def test_complete_shift_version_can_be_saved(self):
        response = self.post('shifts', {'code': 'NEW', 'name': '新班次', 'start': '09:00', 'end': '13:00',
            'hours': 4, 'working': True, 'next_day': False, 'effective_from': '2026-11-01'})
        self.assertEqual(response.status_code, 200)

    def test_nonworking_shift_cannot_accrue_hours(self):
        response = self.post('shifts', {'code': 'BREAK', 'name': '休息', 'start': '09:00', 'end': '13:00',
            'hours': 4, 'working': False, 'next_day': False, 'effective_from': '2026-11-01'})
        self.assertEqual(response.status_code, 400)

    def test_boolean_revision_is_not_version_one(self):
        nurse = Nurse.objects.first()
        response = self.client.patch(f'/api/v1/nurses/{nurse.pk}/',
            {'group': self.group.pk, 'revision': True, 'title': '不应保存'}, format='json')
        self.assertEqual(response.status_code, 409)
        nurse.refresh_from_db()
        self.assertNotEqual(nurse.title, '不应保存')

    def test_invalid_group_id_is_rejected(self):
        self.assertEqual(self.client.get('/api/v1/workspace/', {'group': 'invalid'}).status_code, 400)

    def test_patient_confirmation_requires_boolean_true(self):
        data = self.patient_data()
        for value in ['false', 'true', 1, False]:
            with self.subTest(value=value):
                data['confirmed'] = value
                self.assertEqual(self.post('beds/patient', data).status_code, 400)

    def test_patient_field_shape_is_checked(self):
        data = self.patient_data(); data['fields'] = ['not an object']
        self.assertEqual(self.post('beds/patient', data).status_code, 400)

    def extraction(self):
        with patch.dict(os.environ, {'AI_EXTERNAL_ENABLED': 'false'}):
            response = self.post('beds/extract', {'text': '姓名：合成患者；护理负荷：较轻；诊断：观察',
                'bed_number': 8, 'mode': 'single', 'synthetic': True})
        self.assertEqual(response.status_code, 200)
        return response.data['data']

    def test_extraction_review_rejects_non_object_records(self):
        extraction = self.extraction()
        response = self.post('beds/review', {'extraction': extraction['id'], 'confirmed': True, 'records': [None]})
        self.assertEqual(response.status_code, 400)

    def test_extraction_review_requires_boolean_confirmation(self):
        extraction = self.extraction()
        response = self.post('beds/review', {'extraction': extraction['id'], 'confirmed': 'false',
            'records': extraction['records']})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Patient.objects.count(), 7)

    def test_three_bed_confirmation_requires_boolean_true(self):
        draft = BedDraft.objects.get()
        draft.revision += 1; draft.save()
        self.assertTrue(beds.validate(draft)['warnings'])
        data = {'id': draft.pk, 'revision': draft.revision, 'fingerprint': beds.fingerprint(draft),
                'confirm_three': 'false'}
        count = BedPublication.objects.count()
        self.assertEqual(self.post('beds/publish', data).status_code, 400)
        self.assertEqual(BedPublication.objects.count(), count)
        data['confirm_three'] = True
        self.assertEqual(self.post('beds/publish', data).status_code, 200)

    def test_swap_approval_checks_partners_existing_request(self):
        first, second = list(Nurse.objects.order_by('id')[:2])
        ShiftRequest.objects.create(nurse=second, date='2026-10-29', kind='leave', target_shift='OFF',
                                    reason='合成请假', decision='approved')
        pending = ShiftRequest.objects.create(nurse=first, partner=second, date='2026-10-29', kind='swap',
            target_shift='A', reason='合成换班', partner_consented=True)
        response = self.post(f'requests/{pending.pk}/decision', {'revision': pending.revision, 'decision': 'approved'})
        self.assertEqual(response.status_code, 400)
        pending.refresh_from_db(); self.assertEqual(pending.decision, 'pending')

    def test_request_approval_checks_participation_in_prior_swap(self):
        first, second = list(Nurse.objects.order_by('id')[:2])
        ShiftRequest.objects.create(nurse=first, partner=second, date='2026-10-29', kind='swap', target_shift='A',
            reason='合成换班', partner_consented=True, decision='approved')
        pending = ShiftRequest.objects.create(nurse=second, date='2026-10-29', kind='leave', target_shift='OFF', reason='合成请假')
        response = self.post(f'requests/{pending.pk}/decision', {'revision': pending.revision, 'decision': 'approved'})
        self.assertEqual(response.status_code, 400)

    def test_consent_only_for_pending_swaps(self):
        first, second = list(Nurse.objects.order_by('id')[:2])
        for kind, decision in [('swap', 'rejected'), ('swap', 'approved'), ('change', 'pending')]:
            with self.subTest(kind=kind, decision=decision):
                obj = ShiftRequest.objects.create(nurse=second, partner=first, date='2026-10-29',
                    kind=kind, decision=decision, target_shift='A', reason='合成申请')
                response = self.post(f'requests/{obj.pk}/consent', {'revision': obj.revision, 'confirmed':True})
                self.assertEqual(response.status_code, 409)
                obj.refresh_from_db(); self.assertFalse(obj.partner_consented)

    def test_normal_bed_count_is_fixed_at_two(self):
        response = self.client.patch('/api/v1/settings/', {'group': self.group.pk,
            'revision': self.group.revision, 'rules': {'normal_beds': 1}}, format='json')
        self.assertEqual(response.status_code, 400)
        self.group.refresh_from_db(); self.assertEqual(self.group.rules['normal_beds'], 2)

    def test_workspace_does_not_mark_real_patient_as_demo(self):
        Patient.objects.filter(pk=Patient.objects.first().pk).update(synthetic=False)
        response = self.client.get('/api/v1/workspace/', {'date': str(DAY)})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['data']['synthetic_demo'])

    def test_solver_detects_patient_change_while_solving(self):
        draft = BedDraft.objects.get()
        original_solve = beds.cp_model.CpSolver.Solve
        def concurrent_change(solver, model):
            result = original_solve(solver, model)
            Patient.objects.filter(pk=Patient.objects.first().pk).update(revision=F('revision') + 1)
            return result
        with patch.object(beds.cp_model.CpSolver, 'Solve', concurrent_change):
            with self.assertRaises(BusinessError) as caught:
                beds.recommend(draft)
        self.assertEqual(caught.exception.status_code, 409)

    def test_qualification_edit_invalidates_drafts_without_changing_releases(self):
        nurse=Nurse.objects.first();sd=ScheduleDraft.objects.get();bd=BedDraft.objects.get()
        old_schedule=SchedulePublication.objects.get().snapshot
        old_beds=BedPublication.objects.get().snapshot
        response=self.client.patch(f'/api/v1/nurses/{nurse.id}/',{'group':self.group.id,
            'revision':nurse.revision,'qualifications':['DAY']},format='json')
        self.assertEqual(response.status_code,200)
        old_sd,old_bd=sd.revision,bd.revision;sd.refresh_from_db();bd.refresh_from_db()
        self.assertEqual(sd.revision,old_sd+1);self.assertEqual(bd.revision,old_bd+1)
        self.assertEqual(SchedulePublication.objects.get().snapshot,old_schedule)
        self.assertEqual(BedPublication.objects.get().snapshot,old_beds)

    def test_rule_change_cannot_reuse_previous_publication_revision(self):
        sd=ScheduleDraft.objects.get();before=sd.revision
        self.group.refresh_from_db()
        response=self.client.patch('/api/v1/settings/',{'group':self.group.id,'revision':self.group.revision,
            'rules':{'min_staff':{'A':99,'DAY':0,'N':3}}},format='json')
        self.assertEqual(response.status_code,200);sd.refresh_from_db()
        self.assertEqual(sd.revision,before+1)
        response=self.post('schedules/publish',{'id':sd.id,'revision':sd.revision})
        self.assertEqual(response.status_code,422)
        self.assertEqual(SchedulePublication.objects.count(),1)


class AiRegressionTests(TestCase):
    def test_invalid_mode_and_boolean_bed_number_rejected(self):
        with self.assertRaises(BusinessError): ai.split_records('合成病例资料', 'invalid', 1)
        with self.assertRaises(BusinessError): ai.split_records('合成病例资料', 'single', True)

    def test_single_record_header_is_removed_before_extraction(self):
        number, text = ai.split_records('3床：姓名：合成患者；护理负荷：较轻', 'single', 3)[0]
        self.assertEqual(number, 3)
        self.assertEqual(ai.simulated(text)['name'], '合成患者')

    def test_intubation_fact_is_retained_for_qualification_checks(self):
        result = ai.simulated('当前气管插管；既往机械通气已撤除；拟使用CRRT')
        self.assertIn('气管插管', result['treatments'])
        self.assertNotIn('机械通气', result['treatments'])
        self.assertNotIn('CRRT', result['treatments'])

    def test_mock_preserves_authorized_non_synthetic_flag(self):
        status={'configured':False,'hospital_authorized':True,'model':'unused'}
        with patch.dict(os.environ, {'ICU_AI_ALLOW_MOCK':'true'}), patch('scheduling.services.ai.public_config',return_value=status):
            result, mode = ai.extract('合成文本用于测试真实资料标志', synthetic=False)
        self.assertEqual(mode, 'mock'); self.assertFalse(result['synthetic'])

    def test_external_array_root_returns_controlled_error(self):
        response = Mock()
        response.json.return_value = {'choices': [{'finish_reason':'stop', 'message': {'content': '[]'}}]}
        config={'configured':True,'hospital_authorized':True,'provider':'openai_compatible',
                'base_url':'https://api.example.com/v1','model':'test-model'}
        with patch('scheduling.services.ai.public_config',return_value=config), \
             patch('scheduling.services.ai.secret_value',return_value='test-key'), \
             patch('scheduling.services.ai_configuration.check_external_host',return_value='8.8.8.8'), \
             patch('scheduling.services.ai.httpx.Client.post', return_value=response):
            with self.assertRaises(BusinessError) as caught:
                ai.extract('用于测试的合成病例')
        self.assertEqual(caught.exception.status_code, 502)
