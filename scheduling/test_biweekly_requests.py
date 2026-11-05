"""Biweekly schedule and request revisions use isolated synthetic records."""
from copy import deepcopy
from datetime import date, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo
from datetime import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from scheduling.models import AccountProfile, AuditLog, CareGroup, Nurse, SchedulePublication, ShiftRequest, ShiftType
from scheduling.services import beds, schedule, schedule_cancellation, shift_requests
from scheduling.services.common import BusinessError


class BiweeklyRequestTests(TestCase):
    def setUp(self):
        self.group=CareGroup.objects.create(name='双周合成测试组')
        self.group.rules.update(anchor='2026-10-19',phases=[0,1,2,3],min_staff={'A':0,'DAY':0,'N':0})
        self.group.save()
        self.user=get_user_model().objects.create_user('biweekly-manager')
        AccountProfile.objects.create(user=self.user,role='group_manager',group=self.group)
        self.client=APIClient();self.client.force_authenticate(self.user)
        for code,start,end,working,next_day in [('A','08:00','17:00',True,False),('DAY','08:00','17:00',True,False),
                ('N','17:00','08:00',True,True),('POST','08:00','08:00',False,False),('OFF','00:00','00:00',False,False)]:
            ShiftType.objects.create(group=self.group,code=code,name=code,start=start,end=end,working=working,next_day=next_day)
        self.nurses=[Nurse.objects.create(group=self.group,name=f'测试护士{i}',employee_id=f'SYN-BW{i}',
            subgroup=i+1,qualifications=['A','DAY','N'],status='active') for i in range(4)]
        self.start=date(2026,10,26)

    def approved(self,nurse,day):
        return ShiftRequest.objects.create(group=self.group,nurse=nurse,date=day,kind='change',
            target_shift='OFF',reason='合成申请',decision='approved',reviewer=self.user)

    def test_fourteen_days_cover_cross_month_and_both_weeks_requests(self):
        first=self.approved(self.nurses[0],self.start)
        second=self.approved(self.nurses[1],self.start+timedelta(days=8))
        withdrawn=ShiftRequest.objects.create(group=self.group,nurse=self.nurses[2],date=self.start+timedelta(days=9),
            kind='change',target_shift='OFF',reason='已撤销',decision='withdrawn')
        draft=schedule.generate(self.group,self.user,self.start,duration_days=14)
        self.assertEqual(draft.duration_days,14)
        self.assertEqual(len(draft.entries),56)
        self.assertEqual({e['date'] for e in draft.entries},
                         {str(self.start+timedelta(days=i)) for i in range(14)})
        self.assertEqual({e.get('request_id') for e in draft.entries if e.get('request_id')},{first.id,second.id})
        self.assertNotIn(withdrawn.id,{e.get('request_id') for e in draft.entries})
        self.nurses[3].refresh_from_db()
        for index in (6,7,13):
            day=self.start+timedelta(days=index)
            entry=next(e for e in draft.entries if e['nurse']==self.nurses[3].id and e['date']==str(day))
            self.assertEqual(entry['original'],schedule.base_shift(self.nurses[3],day,self.group.rules))
        summary=draft.generation_versions.get(revision=draft.revision).summary
        self.assertEqual(summary['days'],14);self.assertEqual(summary['saved_cells'],56)
        self.assertEqual({x['id'] for x in summary['request_snapshot']},{first.id,second.id})
        self.assertEqual(schedule.draft_data(draft)['end_date'],'2026-11-08')

    def test_published_biweekly_is_visible_each_day_and_partial_replacement_is_scoped(self):
        draft=schedule.generate(self.group,self.user,self.start,duration_days=14)
        pub=schedule.publish(draft,self.user,{'revision':draft.revision})
        self.assertEqual(pub.duration_days,14)
        for i in range(14):
            day=self.start+timedelta(days=i)
            self.assertEqual(schedule.current_publication(self.group,day).id,pub.id)
            self.assertEqual(len(schedule.published_entries(self.group,day)),4)
            for code in ('A','DAY','N'):
                _,nurses=beds.on_duty(self.group,day,code)
                expected={e['nurse'] for e in pub.snapshot['entries'] if e['date']==str(day) and e['shift']==code}
                self.assertEqual({n.id for n in nurses},expected)
        old=deepcopy(pub.snapshot)
        week2=schedule.generate(self.group,self.user,self.start+timedelta(days=7))
        newer=schedule.publish(week2,self.user,{'revision':week2.revision})
        self.assertEqual(schedule.current_publication(self.group,self.start).id,pub.id)
        self.assertEqual(schedule.current_publication(self.group,self.start+timedelta(days=7)).id,newer.id)
        self.assertEqual(schedule.publication_status(pub)['status'],'partially_replaced')
        response=self.client.get('/api/v1/schedules/period/',{'group':self.group.id,'start_date':str(self.start)})
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(response.data['data']['publication_ids'],[pub.id,newer.id])
        for i in range(14):
            day=self.start+timedelta(days=i)
            self.assertEqual([entry for entry in response.data['data']['entries'] if entry['date']==str(day)],
                             schedule.published_entries(self.group,day))
        pub.refresh_from_db();self.assertEqual(pub.snapshot,old)

    def test_cross_week_rest_issue_is_reported_and_blocks_publish(self):
        draft=schedule.generate(self.group,self.user,self.start,duration_days=14)
        entries=[e for e in draft.entries if e['nurse']==self.nurses[0].id]
        entries[6]['shift']='N';entries[7]['shift']='A'
        draft.save()
        report=schedule.validate(draft)
        self.assertTrue(any(x.get('period')=='cross_week' for x in report['errors']))
        with self.assertRaises(BusinessError):schedule.publish(draft,self.user,{'revision':draft.revision})

    def test_edit_last_day_of_biweekly_draft_and_reject_out_of_period(self):
        draft=schedule.generate(self.group,self.user,self.start,duration_days=14)
        day=self.start+timedelta(days=13)
        response=self.client.post('/api/v1/schedules/edit/',{'group':self.group.id,'id':draft.id,
            'revision':draft.revision,'nurse':self.nurses[0].id,'date':str(day),'shift':'OFF','reason':'合成调整'},format='json')
        self.assertEqual(response.status_code,200,response.data)
        draft.refresh_from_db()
        entry=next(e for e in draft.entries if e['nurse']==self.nurses[0].id and e['date']==str(day))
        self.assertEqual(entry['shift'],'OFF');self.assertEqual(entry['source'],'manager_edit')
        response=self.client.post('/api/v1/schedules/edit/',{'group':self.group.id,'id':draft.id,
            'revision':draft.revision,'nurse':self.nurses[0].id,'date':str(day+timedelta(days=1)),
            'shift':'OFF','reason':'合成调整'},format='json')
        self.assertEqual(response.status_code,400)

    def test_pending_edit_stale_revision_and_withdraw(self):
        req=ShiftRequest.objects.create(group=self.group,nurse=self.nurses[0],date=self.start,
            kind='day',target_shift='DAY',reason='待审核')
        body={'group':self.group.id,'revision':req.revision,'nurse':self.nurses[0].id,
              'date':'2026-11-02','kind':'change','target_shift':'OFF','reason':'修订原因'}
        response=self.client.post(f'/api/v1/requests/{req.id}/edit/',body,format='json')
        self.assertEqual(response.status_code,200,response.data)
        req.refresh_from_db();self.assertEqual(str(req.date),'2026-11-02');self.assertEqual(req.revision,2)
        self.assertEqual(self.client.post(f'/api/v1/requests/{req.id}/edit/',body,format='json').status_code,409)
        res=self.client.post(f'/api/v1/requests/{req.id}/withdraw/',{'group':self.group.id,
            'revision':req.revision,'reason':'需求取消','confirmed':True},format='json')
        self.assertEqual(res.status_code,200,res.data);req.refresh_from_db()
        self.assertEqual(req.decision,'withdrawn');self.assertEqual(req.withdrawal_reason,'需求取消')
        self.assertEqual(self.client.post(f'/api/v1/requests/{req.id}/withdraw/',{'group':self.group.id,
            'revision':req.revision,'reason':'重复','confirmed':True},format='json').status_code,409)
        self.assertTrue(AuditLog.objects.filter(group=self.group,action='编辑调班申请').exists())

    def test_legacy_draft_is_marked_stale_after_request_change(self):
        draft=schedule.generate(self.group,self.user,self.start,duration_days=14)
        draft.request_snapshot=None;draft.save(update_fields=['request_snapshot'])
        response=self.client.post('/api/v1/requests/create/',{'group':self.group.id,'nurse':self.nurses[0].id,
            'date':str(self.start+timedelta(days=8)),'kind':'change','target_shift':'OFF','reason':'合成申请'},format='json')
        self.assertEqual(response.status_code,200,response.data)
        draft.refresh_from_db();self.assertTrue(schedule.draft_data(draft)['request_review_required'])
        self.assertTrue(any('申请发生变化' in error['message'] for error in schedule.validate(draft)['errors']))

    def test_approved_revision_requires_review_and_rejection_preserves_previous(self):
        original=self.approved(self.nurses[0],self.start)
        body={'group':self.group.id,'revision':original.revision,'nurse':self.nurses[0].id,
              'date':str(self.start),'kind':'change','target_shift':'A','reason':'修订'}
        response=self.client.post(f'/api/v1/requests/{original.id}/edit/',body,format='json')
        self.assertEqual(response.status_code,200,response.data)
        revised=ShiftRequest.objects.get(pk=response.data['data']['id'])
        self.assertEqual(revised.decision,'pending');self.assertEqual(revised.revises_id,original.id)
        self.assertEqual(list(shift_requests.effective_requests(self.group,self.start,self.start)),[original])
        response=self.client.post(f'/api/v1/requests/{revised.id}/decision/',{'group':self.group.id,
            'revision':revised.revision,'decision':'rejected'},format='json')
        self.assertEqual(response.status_code,200,response.data)
        original.refresh_from_db();self.assertIsNone(original.superseded_by_id)
        response=self.client.post(f'/api/v1/requests/{original.id}/edit/',body,format='json')
        self.assertEqual(response.status_code,200,response.data)
        replacement=ShiftRequest.objects.get(pk=response.data['data']['id'])
        response=self.client.post(f'/api/v1/requests/{replacement.id}/decision/',{'group':self.group.id,
            'revision':replacement.revision,'decision':'approved'},format='json')
        self.assertEqual(response.status_code,200,response.data)
        original.refresh_from_db();self.assertEqual(original.superseded_by_id,replacement.id)
        self.assertEqual(original.decision,'approved')
        self.assertEqual(list(shift_requests.effective_requests(self.group,self.start,self.start)),[replacement])
        self.assertEqual(self.client.get(f'/api/v1/requests/{original.id}/versions/',{'group':self.group.id}).status_code,200)

    def test_withdrawal_invalidates_draft_preserves_publication_and_new_generation_excludes_request(self):
        req=self.approved(self.nurses[0],self.start)
        draft=schedule.generate(self.group,self.user,self.start,duration_days=14)
        pub=schedule.publish(draft,self.user,{'revision':draft.revision});snapshot=deepcopy(pub.snapshot)
        result=self.client.post(f'/api/v1/requests/{req.id}/withdraw/',{'group':self.group.id,
            'revision':req.revision,'reason':'合成撤销','confirmed':True},format='json')
        self.assertEqual(result.status_code,200,result.data)
        self.assertIn(pub.id,result.data['data']['impact']['publication_ids'])
        draft.refresh_from_db();report=schedule.validate(draft)
        self.assertFalse(report['valid']);self.assertTrue(any('申请' in e['message'] for e in report['errors']))
        with self.assertRaises(BusinessError):schedule.publish(draft,self.user,{'revision':draft.revision})
        pub.refresh_from_db();self.assertEqual(pub.snapshot,snapshot)
        new=schedule.generate(self.group,self.user,self.start,revision=draft.revision,duration_days=14)
        self.assertFalse(any(e.get('request_id')==req.id for e in new.entries))

    def test_permissions_and_duration_validation(self):
        req=self.approved(self.nurses[0],self.start)
        other=CareGroup.objects.create(name='其他合成组')
        user=get_user_model().objects.create_user('other-manager')
        AccountProfile.objects.create(user=user,role='group_manager',group=other)
        self.client.force_authenticate(user)
        self.assertEqual(self.client.post(f'/api/v1/requests/{req.id}/withdraw/',{'group':self.group.id,
            'revision':req.revision,'reason':'无权','confirmed':True},format='json').status_code,403)
        self.assertEqual(self.client.get('/api/v1/schedules/period/',{'group':self.group.id,
            'start_date':str(self.start)}).status_code,403)
        with self.assertRaises(BusinessError):schedule.generate(self.group,self.user,self.start,duration_days=8)

    def test_default_week_and_cross_year_continuity(self):
        weekly=schedule.generate(self.group,self.user,self.start)
        self.assertEqual(weekly.duration_days,7);self.assertEqual(len(weekly.entries),28)
        start=date(2026,12,28)
        biweekly=schedule.generate(self.group,self.user,start,duration_days=14)
        self.assertEqual(schedule.draft_data(biweekly)['end_date'],'2027-01-10')
        self.nurses[0].refresh_from_db()
        for day in (date(2027,1,3),date(2027,1,4),date(2027,1,10)):
            entry=next(e for e in biweekly.entries if e['nurse']==self.nurses[0].id and e['date']==str(day))
            self.assertEqual(entry['shift'],schedule.base_shift(self.nurses[0],day,self.group.rules))

    def test_conflicting_approved_requests_are_not_randomly_applied(self):
        self.approved(self.nurses[0],self.start)
        self.approved(self.nurses[0],self.start)
        with self.assertRaises(BusinessError):schedule.generate(self.group,self.user,self.start,duration_days=14)
        self.assertFalse(self.group.scheduledraft_set.exists())

    def test_cancellation_selects_second_week_of_biweekly_publication(self):
        with patch('django.utils.timezone.localtime',return_value=datetime(2026,10,25,12,tzinfo=ZoneInfo('Asia/Shanghai'))):
            draft=schedule.generate(self.group,self.user,self.start,duration_days=14)
            pub=schedule.publish(draft,self.user,{'revision':draft.revision})
            day=self.start+timedelta(days=8)
            data={'start_date':str(day),'end_date':str(day),'nurse_scope':'all','shift':'ALL',
                  'formal_only':True,'publication_id':pub.id,'reason':'合成测试'}
            preview=schedule_cancellation.preview(self.group,data)
            self.assertEqual(preview['published_records'],4)
            schedule_cancellation.cancel(self.group,self.user,{**data,'fingerprint':preview['fingerprint'],'confirmed':True})
            self.assertEqual(schedule.published_entries(self.group,day),[])
            self.assertEqual(len(schedule.published_entries(self.group,self.start)),4)
            self.assertEqual(len(pub.snapshot['entries']),56)

    def test_stream_reports_actual_phases_and_saved_result(self):
        response=self.client.post('/api/v1/schedules/generate-stream/',{'group':self.group.id,
            'start_date':str(self.start),'duration_days':14,'include_requests':True,'rotation_only':False},format='json')
        self.assertEqual(response.status_code,200)
        body=b''.join(response.streaming_content).decode()
        self.assertIn('正在读取护士资料',body);self.assertIn('草稿已保存',body);self.assertIn('"type": "done"',body)
        self.assertEqual(self.group.scheduledraft_set.get().duration_days,14)
        self.client.force_authenticate(None)
        self.assertIn(self.client.post('/api/v1/schedules/generate-stream/',{'group':self.group.id},format='json').status_code,(401,403))
