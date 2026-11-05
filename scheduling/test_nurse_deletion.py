import copy
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from scheduling.models import (AccountProfile, AuditLog, BedDraft, BedPublication, CareGroup, Nurse,
    NurseDeletionSnapshot, NurseSkill, ScheduleDraft, SchedulePublication, ShiftRequest)
from scheduling.services import workload


DAY=date(2026,10,22)


class NurseDeletionAndAdminRequestTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with patch('django.utils.timezone.localdate',return_value=DAY): call_command('seed_demo',verbosity=0)
        cls.group=CareGroup.objects.get(name='第3组')
        cls.manager=get_user_model().objects.get(username='group3')
        cls.head=get_user_model().objects.get(username='head_nurse')

    def setUp(self):
        self.client=APIClient();self.client.force_authenticate(self.manager)

    def delete(self,nurse,**extra):
        return self.client.delete(f'/api/v1/nurses/{nurse.id}/',
            {'group':self.group.id,'revision':nurse.revision,**extra},format='json')

    def test_real_delete_retains_releases_request_and_workload_facts(self):
        schedule=SchedulePublication.objects.get();bedpub=BedPublication.objects.get()
        assigned=bedpub.snapshot['assignments'][0]['nurse']
        nurse=Nurse.objects.get(pk=assigned);identity=nurse.id
        request=ShiftRequest.objects.create(group=self.group,nurse=nurse,date='2026-10-23',kind='day',
            target_shift='DAY',reason='合成待处理申请',submitted_by=self.manager)
        legacy=get_user_model().objects.create_user('legacy-personal',password='test',is_active=False)
        AccountProfile.objects.create(user=legacy,role='disabled_nurse',group=self.group,nurse=nurse)
        old_schedule=copy.deepcopy(schedule.snapshot);old_bed=copy.deepcopy(bedpub.snapshot)
        before=workload.summary(self.group,DAY)
        response=self.delete(nurse);self.assertEqual(response.status_code,200)
        self.assertFalse(Nurse.objects.filter(pk=identity).exists())
        self.assertFalse(NurseSkill.objects.filter(nurse_id=identity).exists())
        request.refresh_from_db();self.assertIsNone(request.nurse_id);self.assertEqual(request.group,self.group)
        self.assertEqual(request.nurse_id_at_request,identity);self.assertEqual(request.nurse_name_at_request,nurse.name)
        self.assertTrue(request.invalidated_by_deletion)
        schedule.refresh_from_db();bedpub.refresh_from_db()
        self.assertEqual(schedule.snapshot,old_schedule);self.assertEqual(bedpub.snapshot,old_bed)
        self.assertFalse(any(e['nurse']==identity for e in ScheduleDraft.objects.get().entries))
        draft=BedDraft.objects.get();self.assertNotIn(identity,draft.roster);self.assertNotIn(identity,draft.assignments.values())
        legacy.refresh_from_db();legacy.care_profile.refresh_from_db()
        self.assertFalse(legacy.is_active);self.assertIsNone(legacy.care_profile.nurse_id)
        deletion=NurseDeletionSnapshot.objects.get(nurse_id_at_deletion=identity)
        self.assertEqual(deletion.actor,self.manager);self.assertEqual(deletion.snapshot['name'],nurse.name)
        self.assertTrue(all({'id','code','name','level','independent','expires_on'}<=set(x) for x in deletion.snapshot['skill_details']))
        with self.assertRaises(ValidationError): deletion.save()
        self.assertEqual(AuditLog.objects.latest('id').action,'删除护士档案')
        after=workload.summary(self.group,DAY)
        self.assertEqual(after['total_shifts'],before['total_shifts'])
        self.assertEqual(after['patients'],before['patients'])
        self.assertFalse(any(row['id']==identity for row in after['rows']))
        history=next(row for row in after['historical_rows'] if row['id']==identity)
        original=next(row for row in before['rows'] if row['id']==identity)
        self.assertEqual(history['total_shifts'],original['total_shifts']);self.assertEqual(history['patients'],original['patients'])
        workspace=self.client.get('/api/v1/workspace/',{'date':str(DAY)}).data['data']
        item=next(x for x in workspace['requests'] if x['id']==request.id)
        self.assertEqual(item['status'],'invalidated');self.assertEqual(item['name'],nurse.name)
        self.assertEqual(self.client.post(f'/api/v1/requests/{request.id}/decision/',
            {'group':self.group.id,'revision':request.revision,'decision':'approved'},format='json').status_code,409)

    def test_delete_stale_or_other_group_is_rejected(self):
        nurse=Nurse.objects.first();other=CareGroup.objects.create(name='另组')
        self.assertEqual(self.delete(nurse,revision=nurse.revision+1).status_code,409)
        self.assertEqual(self.delete(nurse,group=other.id).status_code,403)
        self.client.force_authenticate(self.head);self.assertEqual(self.delete(nurse).status_code,403)
        self.assertTrue(Nurse.objects.filter(pk=nurse.id).exists());self.assertFalse(NurseDeletionSnapshot.objects.exists())

    def test_avatar_removed_only_after_successful_deletion_commit(self):
        nurse=Nurse.objects.first();key='a'*32+'.webp'
        with tempfile.TemporaryDirectory() as directory, override_settings(NURSE_AVATAR_ROOT=directory):
            portrait=Path(directory)/key;portrait.write_bytes(b'synthetic avatar')
            nurse.avatar_key=key;nurse.save(update_fields=['avatar_key'])
            self.assertEqual(self.delete(nurse,revision=nurse.revision+1).status_code,409)
            self.assertTrue(portrait.exists())
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.delete(nurse).status_code,200)
                self.assertTrue(portrait.exists())
            self.assertFalse(portrait.exists())

    def test_manager_records_application_swap_consent_and_review(self):
        nurse,partner=list(Nurse.objects.order_by('id')[:2])
        data={'group':self.group.id,'nurse':nurse.id,'date':'2026-11-04','kind':'swap',
              'target_shift':'A','partner_employee_id':partner.employee_id,'reason':'已向双方核实的合成申请'}
        response=self.client.post('/api/v1/requests/create/',data,format='json')
        self.assertEqual(response.status_code,200);entry=response.data['data'];self.assertEqual(entry['submitted_by'],'group3')
        url=f"/api/v1/requests/{entry['id']}/consent/"
        self.assertEqual(self.client.post(url,{'group':self.group.id,'revision':entry['revision'],'confirmed':False},format='json').status_code,400)
        response=self.client.post(url,{'group':self.group.id,'revision':entry['revision'],'confirmed':True,'note':'对方已同意'},format='json')
        self.assertEqual(response.status_code,200);entry=response.data['data'];self.assertEqual(entry['partner_confirmed_by'],'group3')
        response=self.client.post(f"/api/v1/requests/{entry['id']}/decision/",{'group':self.group.id,
            'revision':entry['revision'],'decision':'approved','note':'审核通过'},format='json')
        self.assertEqual(response.status_code,200);self.assertEqual(response.data['data']['decision'],'approved')
        self.assertEqual(ShiftRequest.objects.get(pk=entry['id']).reviewer,self.manager)

    def test_delete_one_swap_participant_removes_invalid_approval_from_surviving_draft(self):
        first,second=list(Nurse.objects.order_by('id')[:2]);draft=ScheduleDraft.objects.get()
        request=ShiftRequest.objects.create(group=self.group,nurse=first,partner=second,date='2026-10-22',
            kind='swap',target_shift='A',reason='合成换班',decision='approved',partner_consented=True)
        entry=next(e for e in draft.entries if e['nurse']==first.id and e['date']=='2026-10-22')
        entry.update(source='approved_request',request_id=request.id,shift='DAY',original='A');draft.save()
        self.assertEqual(self.delete(second).status_code,200)
        draft.refresh_from_db();entry=next(e for e in draft.entries if e['nurse']==first.id and e['date']=='2026-10-22')
        self.assertEqual(entry['shift'],'A');self.assertNotIn('request_id',entry)
        self.assertEqual(entry['source'],'automatic')
        request.refresh_from_db();self.assertEqual(request.decision,'approved');self.assertTrue(request.invalidated_by_deletion)

    def test_disabled_personal_account_cannot_login_or_access_endpoints(self):
        nurse=Nurse.objects.first()
        user=get_user_model().objects.create_user('old-nurse',password='old-password',is_active=False)
        AccountProfile.objects.create(user=user,role='disabled_nurse',group=self.group,nurse=nurse)
        client=APIClient();response=client.post('/api/v1/auth/login/',{'username':'old-nurse','password':'old-password'},format='json')
        self.assertIn(response.status_code,[400,403])
        client.force_authenticate(user)
        self.assertEqual(client.get('/api/v1/workspace/').status_code,403)
        self.assertEqual(client.get('/api/v1/auth/me/').status_code,403)
        self.assertEqual(client.post('/api/v1/requests/create/',{'group':self.group.id},format='json').status_code,403)
