from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from scheduling.models import AccountProfile, CareGroup, Nurse, ScheduleDraft, SchedulePublication, ShiftRequest


class RequestHistoryTests(TestCase):
    def setUp(self):
        self.group=CareGroup.objects.create(name='申请合成测试组')
        self.other=CareGroup.objects.create(name='其他测试组')
        self.user=get_user_model().objects.create_user('history-manager',password='test-password')
        AccountProfile.objects.create(user=self.user,role='group_manager',group=self.group)
        self.nurse=Nurse.objects.create(group=self.group,name='合成护士',employee_id='SYN-HISTORY')
        self.today=timezone.localdate();past=self.today-timedelta(days=1)
        self.rejected=ShiftRequest.objects.create(group=self.group,nurse=self.nurse,date=past,kind='change',target_shift='A',reason='合成',decision='rejected')
        self.pending=ShiftRequest.objects.create(group=self.group,nurse=self.nurse,date=self.today,kind='change',target_shift='A',reason='合成')
        self.approved=ShiftRequest.objects.create(group=self.group,nurse=self.nurse,date=past,kind='change',target_shift='A',reason='合成',decision='approved')
        self.future=ShiftRequest.objects.create(group=self.group,nurse=self.nurse,date=self.today+timedelta(days=1),kind='change',target_shift='A',reason='合成',decision='approved')
        draft=ScheduleDraft.objects.create(group=self.group,start_date=past,entries=[])
        self.publication=SchedulePublication.objects.create(group=self.group,draft=draft,draft_revision=1,start_date=past,published_by=self.user,
            snapshot={'entries':[{'nurse':self.nurse.id,'date':str(past),'shift':'A','request_id':self.approved.id}]})
        self.client=APIClient();self.client.force_authenticate(self.user)

    def post(self,**values):return self.client.post('/api/v1/requests/history/',{'group':self.group.id,**values},format='json')
    def test_preview_delete_restore_preserves_approval_and_publication(self):
        chosen=[{'id':x.id,'revision':x.revision} for x in [self.rejected,self.approved,self.pending,self.future]]
        preview=self.post(action='delete',items=chosen,preview=True)
        self.assertEqual(preview.status_code,200);self.assertEqual(preview.data['data']['eligible'],2)
        denied=self.post(action='delete',items=chosen,confirmed=True);self.assertEqual(denied.status_code,409)
        self.assertFalse(ShiftRequest.objects.filter(is_deleted=True).exists())
        result=self.post(action='delete',items=chosen[:2],confirmed=True)
        self.assertEqual(result.status_code,200);self.approved.refresh_from_db();self.assertTrue(self.approved.is_deleted)
        self.assertEqual(self.approved.decision,'approved');self.publication.refresh_from_db()
        self.assertEqual(self.publication.snapshot['entries'][0]['request_id'],self.approved.id)
        deleted=self.client.get('/api/v1/requests/history/',{'group':self.group.id,'state':'deleted'}).data['data']
        self.assertEqual(len(deleted),2)
        workspace=self.client.get('/api/v1/workspace/',{'group':self.group.id}).data['data']
        self.assertEqual(workspace['pending_request_count'],1);self.assertNotIn(self.approved.id,[r['id'] for r in workspace['requests']])
        restored=self.post(action='restore',items=[{'id':self.approved.id,'revision':self.approved.revision}],confirmed=True)
        self.assertEqual(restored.status_code,200);self.approved.refresh_from_db();self.assertFalse(self.approved.is_deleted)
        self.assertEqual(self.approved.decision,'approved')

    def test_stale_and_cross_group_access_are_rejected(self):
        stale=self.post(action='delete',items=[{'id':self.rejected.id,'revision':99}],confirmed=True)
        self.assertEqual(stale.status_code,409)
        denied=self.client.post('/api/v1/requests/history/',{'group':self.other.id,'action':'delete','items':[{'id':self.rejected.id,'revision':1}],'confirmed':True},format='json')
        self.assertEqual(denied.status_code,403)
