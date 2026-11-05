"""Versioned regeneration, formal cancellation and downstream safety."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from scheduling.models import (AccountProfile, AuditLog, BedDraft, BedPublication, CareGroup, Nurse,
    ScheduleDraft, ScheduleDraftVersion, SchedulePublication, ShiftType)
from scheduling.cancellation_models import ScheduleCancellationHold
from scheduling.services import beds, schedule, schedule_cancellation
from scheduling.services.common import BusinessError


class ScheduleVersionTests(TestCase):
    def setUp(self):
        self.group=CareGroup.objects.create(name='排班版本合成组')
        self.group.rules.update(anchor='2026-10-19',phases=[0,1,2,3],min_staff={'A':7,'DAY':0,'N':7})
        self.group.save()
        self.user=get_user_model().objects.create_user('schedule-version-manager')
        AccountProfile.objects.create(user=self.user,role='group_manager',group=self.group)
        for code,start,end,working,next_day in [('A','08:00','17:00',True,False),('DAY','08:00','17:00',True,False),
                ('N','17:00','08:00',True,True),('POST','08:00','08:00',False,False),('OFF','00:00','00:00',False,False)]:
            ShiftType.objects.create(group=self.group,code=code,name=code,start=start,end=end,working=working,next_day=next_day)
        for index in range(31):
            subgroup=1 if index<7 else 2 if index<15 else 3 if index<23 else 4
            Nurse.objects.create(group=self.group,name=f'合成护士{index}',employee_id=f'SYN-V{index}',
                subgroup=subgroup,qualifications=['A','DAY','N'],status='active')
        self.start=date(2026,10,26)
        self.client=APIClient();self.client.force_authenticate(self.user)

    def test_cancelled_cells_do_not_mask_new_generation_or_its_snapshot(self):
        first=schedule.generate(self.group,self.user,str(self.start));first_id=schedule.draft_data(first)['generation_version_id']
        criteria={'start_date':str(self.start),'end_date':'2026-11-01','nurse_scope':'all','shift':'ALL','reason':'合成测试'}
        preview=schedule_cancellation.preview(self.group,criteria)
        self.assertEqual(preview['draft_records'],217);self.assertEqual(preview['published_records'],0)
        schedule_cancellation.cancel(self.group,self.user,{**criteria,'fingerprint':preview['fingerprint'],'confirmed':True})
        first.refresh_from_db();old_report=schedule.validate(first)
        self.assertEqual(len(first.entries),0);self.assertEqual(len(old_report['errors']),231)
        new=schedule.generate(self.group,self.user,str(self.start),revision=first.revision)
        data=schedule.draft_data(new);self.assertNotEqual(first_id,data['generation_version_id'])
        self.assertEqual(len(new.entries),217);self.assertEqual(len({e['date'] for e in new.entries}),7)
        self.assertEqual(data['generation_summary']['saved_cells'],217)
        self.assertEqual(ScheduleDraftVersion.objects.get(pk=first_id).entries,ScheduleDraftVersion.objects.get(pk=data['generation_version_id']).entries)
        self.assertEqual(ScheduleCancellationHold.objects.filter(group=self.group,active=True).count(),217)
        report=schedule.validate(new);self.assertTrue(report['valid'],report['errors'])
        self.assertTrue(all(row.get('A',0)>=7 and row.get('N',0)>=7 for row in report['counts'].values()))
        self.group.rules['min_staff']['A']=99;self.group.save();new.group=self.group
        self.assertTrue(any('人数不足' in item['message'] for item in schedule.validate(new)['errors']))

    def test_formal_partial_cancel_preserves_draft_and_forces_bed_resync(self):
        before_time=datetime(2026,10,25,12,tzinfo=ZoneInfo('Asia/Shanghai'))
        with patch('django.utils.timezone.localtime',return_value=before_time):
            draft=schedule.generate(self.group,self.user,str(self.start));pub=schedule.publish(draft,self.user,{'revision':draft.revision})
            chosen=next(e for e in pub.snapshot['entries'] if e['date']==str(self.start) and e['shift']=='A')
            old=deepcopy(pub.snapshot);draft_entries=deepcopy(draft.entries)
            bd=BedDraft.objects.create(group=self.group,date=self.start,shift='A',schedule_publication=pub,
                roster=[n.id for n in beds.on_duty(self.group,self.start,'A')[1]])
            old_revision=bd.revision;old_fingerprint=beds.fingerprint(bd)
            criteria={'start_date':str(self.start),'end_date':str(self.start),'nurse_scope':'all',
                'nurse_ids':[chosen['nurse']],'shift':'A','formal_only':True,'publication_id':pub.id,'reason':'合成测试'}
            preview=schedule_cancellation.preview(self.group,criteria)
            self.assertTrue(preview['can_execute']);self.assertEqual(preview['published_records'],1);self.assertEqual(preview['draft_records'],0)
            bd.revision+=1;bd.save(update_fields=['revision'])
            with self.assertRaises(BusinessError) as stale:
                schedule_cancellation.cancel(self.group,self.user,{**criteria,'fingerprint':preview['fingerprint'],'confirmed':True})
            self.assertEqual(stale.exception.status_code,409)
            preview=schedule_cancellation.preview(self.group,criteria)
            schedule_cancellation.cancel(self.group,self.user,{**criteria,'fingerprint':preview['fingerprint'],'confirmed':True})
            pub.refresh_from_db();draft.refresh_from_db();bd.refresh_from_db()
            self.assertEqual(pub.snapshot,old);self.assertEqual(draft.entries,draft_entries)
            self.assertEqual(schedule.publication_status(pub)['status'],'partially_cancelled')
            self.assertNotIn(chosen['nurse'],[n.id for n in beds.on_duty(self.group,self.start,'A')[1]])
            self.assertGreater(bd.revision,old_revision);self.assertNotEqual(beds.fingerprint(bd),old_fingerprint)
            self.assertFalse(beds.validate(bd,{},partial=True)['valid'])
            with self.assertRaises(BusinessError):schedule.publish(draft,self.user,{'revision':draft.revision})
            new=schedule.generate(self.group,self.user,str(self.start),revision=draft.revision)
            newpub=schedule.publish(new,self.user,{'revision':new.revision})
            self.assertNotEqual(newpub.id,pub.id);self.assertEqual(schedule.current_publication(self.group,self.start).id,newpub.id)
            self.assertEqual(schedule.publication_status(pub)['status'],'replaced')
            self.assertIn(chosen['nurse'],[n.id for n in beds.on_duty(self.group,self.start,'A')[1]])
            self.assertFalse(beds.validate(bd,{},partial=True)['valid'])
            beds.sync(bd,self.user,{'revision':bd.revision,'fingerprint':beds.fingerprint(bd)})
            self.assertEqual(bd.schedule_publication_id,newpub.id)

    def test_historical_correction_and_active_bed_handover_are_guarded(self):
        draft=schedule.generate(self.group,self.user,str(self.start));pub=schedule.publish(draft,self.user,{'revision':draft.revision})
        chosen=next(e for e in pub.snapshot['entries'] if e['date']==str(self.start) and e['shift']=='A')
        criteria={'start_date':str(self.start),'end_date':str(self.start),'nurse_scope':'all','shift':'A',
            'nurse_ids':[chosen['nurse']],'formal_only':True,'publication_id':pub.id,'reason':'合成历史更正'}
        noon=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))
        with patch('django.utils.timezone.localtime',return_value=noon):
            preview=schedule_cancellation.preview(self.group,criteria)
            self.assertEqual(preview['protected_records'],1);self.assertFalse(preview['can_execute'])
            with self.assertRaises(BusinessError):schedule_cancellation.cancel(self.group,self.user,{**criteria,'fingerprint':preview['fingerprint'],'confirmed':True})
            bd=BedDraft.objects.create(group=self.group,date=self.start,shift='A',schedule_publication=pub)
            bp=BedPublication.objects.create(group=self.group,draft=bd,draft_revision=1,date=self.start,shift='A',
                snapshot={'assignments':[{'nurse':chosen['nurse']}]},published_by=self.user)
            corrected={**criteria,'historical_correction':True};preview=schedule_cancellation.preview(self.group,corrected)
            self.assertEqual(preview['active_bed_conflicts'],1);self.assertFalse(preview['can_execute'])
            with self.assertRaises(BusinessError):schedule_cancellation.cancel(self.group,self.user,{**corrected,'fingerprint':preview['fingerprint'],'confirmed':True})
            self.assertTrue(BedPublication.objects.filter(pk=bp.id).exists())

    def test_overnight_shift_remains_in_progress_until_next_morning(self):
        entry={'date':'2026-10-26','shift':'N'}
        self.assertEqual(schedule.shift_phase(self.group,entry,datetime(2026,10,27,7,0,tzinfo=ZoneInfo('Asia/Shanghai'))),'in_progress')
        self.assertEqual(schedule.shift_phase(self.group,entry,datetime(2026,10,27,8,0,tzinfo=ZoneInfo('Asia/Shanghai'))),'started')

    def test_cancellation_requires_resync_even_when_available_roster_does_not_change(self):
        future=datetime(2026,10,25,12,tzinfo=ZoneInfo('Asia/Shanghai'))
        with patch('django.utils.timezone.localtime',return_value=future):
            draft=schedule.generate(self.group,self.user,str(self.start));pub=schedule.publish(draft,self.user,{'revision':draft.revision})
            selected=next(e for e in pub.snapshot['entries'] if e['date']==str(self.start) and e['shift']=='A')
            Nurse.objects.filter(pk=selected['nurse']).update(can_manage_beds=False)
            bd=BedDraft.objects.create(group=self.group,date=self.start,shift='A')
            beds.sync(bd,self.user,{'revision':bd.revision,'fingerprint':beds.fingerprint(bd)})
            roster=list(bd.roster);fingerprint=beds.fingerprint(bd)
            data={'start_date':str(self.start),'end_date':str(self.start),'nurse_scope':'all','shift':'A',
                'nurse_ids':[selected['nurse']],'formal_only':True,'publication_id':pub.id,'reason':'合成测试'}
            preview=schedule_cancellation.preview(self.group,data)
            schedule_cancellation.cancel(self.group,self.user,{**data,'fingerprint':preview['fingerprint'],'confirmed':True})
            bd.refresh_from_db();self.assertEqual(bd.roster,roster)
            self.assertTrue(beds.cancellation_after_sync(bd,pub));self.assertNotEqual(beds.fingerprint(bd),fingerprint)
            self.assertTrue(any('重新同步' in e['message'] for e in beds.validate(bd,{},partial=True)['errors']))
            beds.sync(bd,self.user,{'revision':bd.revision,'fingerprint':beds.fingerprint(bd)})
            self.assertFalse(beds.cancellation_after_sync(bd,pub))

    def test_formal_preview_can_select_deleted_nurse_from_immutable_snapshot(self):
        future=datetime(2026,10,25,12,tzinfo=ZoneInfo('Asia/Shanghai'))
        with patch('django.utils.timezone.localtime',return_value=future):
            draft=schedule.generate(self.group,self.user,str(self.start));pub=schedule.publish(draft,self.user,{'revision':draft.revision})
            entry=next(e for e in pub.snapshot['entries'] if e['date']==str(self.start) and e['shift']=='A')
            nurse_id=entry['nurse'];Nurse.objects.get(pk=nurse_id).delete()
            data={'start_date':str(self.start),'end_date':str(self.start),'nurse_scope':'all','shift':'A',
                  'nurse_ids':[nurse_id],'formal_only':True,'publication_id':pub.id,'reason':'合成测试'}
            preview=schedule_cancellation.preview(self.group,data)
            self.assertEqual(preview['published_records'],1);self.assertEqual(preview['affected_members'][0]['id'],nurse_id)
            schedule_cancellation.cancel(self.group,self.user,{**data,'fingerprint':preview['fingerprint'],'confirmed':True})
            self.assertFalse(any(e['nurse']==nurse_id for e in schedule.published_entries(self.group,self.start)))
            self.assertTrue(any(e['nurse']==nurse_id for e in pub.snapshot['entries']))

    def test_unauthorized_or_stale_formal_version_cannot_be_cancelled(self):
        draft=schedule.generate(self.group,self.user,str(self.start));pub=schedule.publish(draft,self.user,{'revision':draft.revision})
        data={'group':self.group.id,'start_date':str(self.start),'end_date':str(self.start),'nurse_scope':'all',
              'shift':'ALL','formal_only':True,'publication_id':pub.id,'reason':'合成测试'}
        stranger=get_user_model().objects.create_user('schedule-readonly');AccountProfile.objects.create(user=stranger,role='head_nurse')
        self.client.force_authenticate(stranger)
        self.assertEqual(self.client.post('/api/v1/schedules/cancel-preview/',data,format='json').status_code,403)
        self.client.force_authenticate(self.user)
        newer=schedule.generate(self.group,self.user,str(self.start),revision=draft.revision)
        schedule.publish(newer,self.user,{'revision':newer.revision})
        self.assertEqual(self.client.post('/api/v1/schedules/cancel-preview/',data,format='json').status_code,409)

    def test_cancel_whole_publication_is_atomic_and_does_not_fall_back_to_older_version(self):
        future=datetime(2026,10,25,12,tzinfo=ZoneInfo('Asia/Shanghai'))
        with patch('django.utils.timezone.localtime',return_value=future):
            draft=schedule.generate(self.group,self.user,str(self.start));old=schedule.publish(draft,self.user,{'revision':draft.revision})
            draft=schedule.generate(self.group,self.user,str(self.start),revision=draft.revision)
            pub=schedule.publish(draft,self.user,{'revision':draft.revision});original=deepcopy(pub.snapshot)
            data={'start_date':str(self.start),'end_date':'2026-11-01','nurse_scope':'all','shift':'ALL',
                  'formal_only':True,'publication_id':pub.id,'reason':'合成整期撤销'}
            preview=schedule_cancellation.preview(self.group,data)
            self.assertEqual(preview['published_records'],217)
            schedule_cancellation.cancel(self.group,self.user,{**data,'fingerprint':preview['fingerprint'],'confirmed':True})
            self.assertEqual(schedule.publication_status(pub)['status'],'cancelled')
            self.assertEqual(schedule.current_publication(self.group,self.start).id,pub.id)
            self.assertEqual(schedule.published_entries(self.group,self.start),[])
            self.assertEqual(pub.snapshot,original);self.assertEqual(len(old.snapshot['entries']),217)
            self.assertEqual(len(draft.entries),217)
            with self.assertRaises(BusinessError):schedule_cancellation.cancel(self.group,self.user,{**data,'fingerprint':preview['fingerprint'],'confirmed':True})
            summary=self.client.get('/api/v1/overview/',{'group':self.group.id}).data['data']
            # The overview uses today's actual calendar day; its status is checked
            # independently on the same publication above.
            self.assertIsInstance(summary,dict)

    def test_historical_publication_change_requires_reason_and_preserves_original(self):
        group=CareGroup.objects.create(name='历史更正合成组')
        group.rules.update(min_staff={'A':0,'DAY':0,'N':0});group.save()
        nurse=Nurse.objects.create(group=group,name='合成护士',employee_id='SYN-HIST',qualifications=['A','DAY'])
        for code,working in [('A',True),('DAY',True),('OFF',False)]:
            ShiftType.objects.create(group=group,code=code,name=code,working=working)
        entries=[{'nurse':nurse.id,'date':str(self.start+timedelta(days=i)),
                  'shift':'DAY' if i==0 else 'OFF','original':'OFF','source':'automatic','reason':''} for i in range(7)]
        draft=ScheduleDraft.objects.create(group=group,start_date=self.start,entries=entries)
        former=deepcopy(entries);former[0]['shift']='A'
        original=SchedulePublication.objects.create(group=group,draft=draft,draft_revision=99,start_date=self.start,
            snapshot={'entries':former},published_by=self.user)
        bed_draft=BedDraft.objects.create(group=group,date=self.start,shift='A',schedule_publication=original)
        bed_publication=BedPublication.objects.create(group=group,draft=bed_draft,draft_revision=1,date=self.start,shift='A',
            snapshot={'assignments':[{'nurse':nurse.id}],'schedule_publication':original.id},published_by=self.user)
        bed_snapshot=deepcopy(bed_publication.snapshot)
        with patch('django.utils.timezone.localtime',return_value=datetime(2026,10,26,10,tzinfo=ZoneInfo('Asia/Shanghai'))):
            with self.assertRaises(BusinessError):schedule.publish(draft,self.user,{'revision':draft.revision,'historical_correction':True,'correction_reason':'核实后更正'})
        with patch('django.utils.timezone.localtime',return_value=datetime(2026,10,30,12,tzinfo=ZoneInfo('Asia/Shanghai'))):
            report=schedule.validate(draft);self.assertTrue(report['valid'],report['errors'])
            self.assertEqual(report['historical_correction_count'],1)
            with self.assertRaises(BusinessError):schedule.publish(draft,self.user,{'revision':draft.revision})
            new=schedule.publish(draft,self.user,{'revision':draft.revision,'historical_correction':True,'correction_reason':'核实后更正'})
        self.assertNotEqual(new.id,original.id);self.assertEqual(original.snapshot['entries'],former)
        bed_publication.refresh_from_db();self.assertEqual(bed_publication.snapshot,bed_snapshot)
        self.assertTrue(AuditLog.objects.filter(group=group,action='发布排班',details__historical_correction=True).exists())
