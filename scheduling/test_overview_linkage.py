from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from scheduling.cancellation_models import ScheduleCancellation, ScheduleCancellationEntry
from scheduling.models import (AccountProfile, Bed, BedDraft, BedPublication, CareGroup, Nurse,
    NurseSkill, Patient, ScheduleDraft, SchedulePublication, ShiftType, Skill)


class OverviewLinkageTests(TestCase):
    def setUp(self):
        self.group=CareGroup.objects.create(name='总览联动合成组')
        self.group.rules.update(min_staff={'A':1,'DAY':0,'N':1},clinical_thresholds_confirmed=False)
        self.group.save()
        self.user=get_user_model().objects.create_user('overview-link-manager')
        AccountProfile.objects.create(user=self.user,role='group_manager',group=self.group)
        self.nurse=Nurse.objects.create(group=self.group,name='合成护士',employee_id='SYN-OV-LINK',
                                        qualifications=['A','DAY','N'],level='N2-1',years=5)
        for code,name,start,end,working,next_day in [
            ('A','A班','08:00','17:00',True,False),('DAY','白班','08:00','17:00',True,False),
            ('N','N班','17:00','08:00',True,True),('POST','夜下休','08:00','08:00',False,False),
            ('OFF','休','00:00','00:00',False,False)]:
            ShiftType.objects.create(group=self.group,code=code,name=name,start=start,end=end,
                                     working=working,next_day=next_day)
        self.now=datetime(2026,10,29,10,tzinfo=ZoneInfo('Asia/Shanghai'))
        self.today=self.now.date();self.week=date(2026,10,26)
        self.client=APIClient();self.client.force_authenticate(self.user)

    def entry(self,nurse=None,day=None,shift='A'):
        return {'nurse':(nurse or self.nurse).id,'date':str(day or self.today),'shift':shift,
                'working':shift not in ('POST','OFF'),'subgroup':1,'base_type':'rotation'}

    def publish(self,entries,revision=1,start=None):
        start=start or self.week
        draft,_=ScheduleDraft.objects.get_or_create(group=self.group,start_date=start,defaults={'entries':entries})
        return SchedulePublication.objects.create(group=self.group,draft=draft,draft_revision=revision,start_date=start,
                                                   snapshot={'entries':entries},published_by=self.user)

    def read(self, now=None):
        with patch('scheduling.services.overview.timezone.localtime',return_value=now or self.now):
            response=self.client.get('/api/v1/overview/',{'group':self.group.id})
        self.assertEqual(response.status_code,200)
        return response.data['data']

    def test_saved_requirements_are_visible_but_unconfirmed_and_zero_is_not_a_target(self):
        self.publish([self.entry()])
        result=self.read(); rows={row['code']:row for row in result['shifts']}
        self.assertEqual((rows['A']['count'],rows['A']['need'],rows['A']['coverage'],rows['A']['status']),(1,1,100.0,'meets'))
        self.assertEqual((rows['N']['count'],rows['N']['need'],rows['N']['coverage'],rows['N']['status']),(0,1,0.0,'insufficient'))
        self.assertIsNone(rows['DAY']['need']);self.assertEqual(rows['DAY']['status'],'unconfigured')
        self.assertEqual(rows['POST']['status'],'rest');self.assertIsNone(rows['POST']['coverage'])
        self.assertFalse(result['staffing']['requirements_confirmed'])
        self.assertEqual(result['staffing']['status'],'insufficient')
        self.assertEqual(result['staffing']['missing_requirements'],['白班'])
        self.assertIsNone(result['admissions']);self.assertIsNone(result['discharges'])
        self.assertEqual(result['data_quality']['attendance'],'unavailable')

    def test_cancel_then_republish_changes_current_week_and_validation(self):
        old=self.publish([self.entry()]);self.assertEqual(self.read()['publication']['id'],old.id)
        cancellation=ScheduleCancellation.objects.create(group=self.group,actor=self.user,reason='合成测试',criteria={},snapshot={})
        ScheduleCancellationEntry.objects.create(group=self.group,cancellation=cancellation,publication=old,
            nurse_id_at_action=self.nurse.id,date=self.today,shift='A')
        cancelled=self.read();self.assertEqual(cancelled['publication']['status'],'cancelled')
        self.assertEqual(cancelled['publication']['effective_records'],0)
        self.assertIsNone(next(row for row in cancelled['shifts'] if row['code']=='A')['count'])
        self.assertEqual(cancelled['validation']['status'],'no_effective_schedule')
        new=self.publish([self.entry()],revision=2)
        current=self.read();self.assertEqual(current['publication']['id'],new.id)
        self.assertEqual(current['publication']['status'],'published')
        self.assertEqual(next(row for row in current['shifts'] if row['code']=='A')['count'],1)
        self.assertEqual(old.snapshot['entries'][0]['shift'],'A')

    def test_partial_cancellation_preserves_unaffected_shift_and_rechecks_rules(self):
        second=Nurse.objects.create(group=self.group,name='合成护士乙',employee_id='SYN-OV-LINK-2',qualifications=['N'])
        pub=self.publish([self.entry(),self.entry(second,shift='N')])
        cancellation=ScheduleCancellation.objects.create(group=self.group,actor=self.user,reason='合成测试',criteria={},snapshot={})
        ScheduleCancellationEntry.objects.create(group=self.group,cancellation=cancellation,publication=pub,
            nurse_id_at_action=self.nurse.id,date=self.today,shift='A')
        result=self.read();rows={r['code']:r for r in result['shifts']}
        self.assertEqual(result['publication']['status'],'partially_cancelled')
        self.assertEqual((rows['A']['count'],rows['N']['count']),(0,1))
        self.assertEqual(result['validation']['status'],'blocking')
        self.assertGreater(result['publication']['anomalies'],0)
        self.assertTrue(any(a['title']=='正式排班部分撤销，待重新校验' for a in result['alerts']))

    def test_completed_validation_can_show_zero_when_there_are_truly_no_findings(self):
        self.group.rules.update(min_staff={'A':0,'DAY':0,'N':0},clinical_thresholds_confirmed=True)
        self.group.save()
        self.publish([self.entry(day=self.week+timedelta(days=i),shift='OFF') for i in range(7)])
        result=self.read()
        self.assertEqual(result['validation']['status'],'passed')
        self.assertEqual(result['publication']['anomalies'],0)

    def test_overlapping_versions_use_current_owner_for_each_date(self):
        monday=self.week;wednesday=self.week+timedelta(days=2)
        old=self.publish([self.entry(day=monday),self.entry(day=wednesday)])
        new=self.publish([self.entry(day=wednesday)],start=wednesday)
        result=self.read();formal=result['publication']
        self.assertEqual(formal['id'],new.id);self.assertTrue(formal['multiple_versions'])
        self.assertEqual(formal['version_ids'],[old.id,new.id])
        self.assertEqual(formal['effective_records'],2)
        self.assertEqual(formal['nurse_count'],1)

    def test_beds_and_skill_qualification_follow_effective_group_data(self):
        hidden=Bed.objects.create(group=self.group,number='101',enabled=True,configured=True,visible=False)
        closed=Bed.objects.create(group=self.group,number='102',enabled=False,configured=True,visible=True)
        patient=Patient.objects.create(group=self.group,bed=hidden,code='SYN-LINK-P',name='合成患者',synthetic=True,reviewed_by=self.user)
        skill=Skill.objects.create(group=self.group,code='CRRT',name='CRRT',enabled=True)
        self.nurse.certifications=[{'name':'CRRT','expires_on':'2030-01-01'}];self.nurse.save()
        NurseSkill.objects.create(nurse=self.nurse,skill=skill,independent=True,expires_on='2030-01-01')
        paused=Nurse.objects.create(group=self.group,name='合成停排护士',employee_id='SYN-OV-PAUSED',status='paused',
            level='N2-2',certifications=[{'name':'CRRT','expires_on':'2030-01-01'}])
        NurseSkill.objects.create(nurse=paused,skill=skill,independent=True,expires_on='2030-01-01')
        future=Nurse.objects.create(group=self.group,name='未来人员',employee_id='SYN-OV-FUTURE',effective_from=date(2030,1,1))
        NurseSkill.objects.create(nurse=future,skill=skill,independent=True,expires_on='2030-01-01')
        result=self.read()
        self.assertEqual((result['beds'],result['patients'],result['synthetic_patients']),(1,1,1))
        self.assertEqual(result['nurses_total'],2);self.assertEqual(result['levels']['N2-2'],1)
        self.assertEqual((result['skills'][0]['skilled'],result['skills'][0]['independent']),(2,1))
        self.assertTrue(any(a['title']=='在床患者所在床位状态异常' for a in result['alerts']))
        NurseSkill.objects.filter(nurse=self.nurse,skill=skill).update(expires_on='2025-01-01')
        self.assertEqual(self.read()['skills'][0]['independent'],0)
        NurseSkill.objects.filter(nurse=self.nurse,skill=skill).update(expires_on='2030-01-01')
        self.nurse.status='paused';self.nurse.save(update_fields=['status'])
        self.assertEqual(self.read()['skills'][0]['independent'],0)
        self.assertFalse(closed.enabled);self.assertIsNotNone(patient.bed_id)

    def test_current_bed_release_is_scoped_to_exact_shift_and_source(self):
        pub=self.publish([self.entry()])
        bed=Bed.objects.create(group=self.group,number='1')
        patient=Patient.objects.create(group=self.group,bed=bed,code='SYN-OV-BED',name='合成患者',reviewed_by=self.user)
        other=BedDraft.objects.create(group=self.group,date=self.today,shift='N',schedule_publication=pub)
        BedPublication.objects.create(group=self.group,draft=other,draft_revision=1,date=self.today,shift='N',
            snapshot={'assignments':[],'schedule_publication':pub.id},published_by=self.user)
        first=self.read();self.assertTrue(any(a['title']=='当前班次床位分配未发布' and a['shift']=='A' for a in first['alerts']))
        draft=BedDraft.objects.create(group=self.group,date=self.today,shift='A',schedule_publication=pub,roster=[self.nurse.id])
        snapshot={'assignments':[{'patient':patient.id,'patient_revision':patient.revision,'bed':bed.id,'nurse':self.nurse.id}],
                  'schedule_publication':pub.id}
        BedPublication.objects.create(group=self.group,draft=draft,draft_revision=1,date=self.today,shift='A',
                                      snapshot=snapshot,published_by=self.user)
        current=self.read();self.assertFalse(any(a['title']=='当前班次床位分配未发布' and a.get('shift')=='A' for a in current['alerts']))
        patient.revision+=1;patient.save(update_fields=['revision'])
        self.assertTrue(any(a['title']=='已发布分床引用的患者资料已变更' for a in self.read()['alerts']))
        patient.treatments=['CRRT'];patient.save(update_fields=['treatments'])
        self.assertTrue(any(a['title']=='特殊治疗资格或护理能力需重新核查' for a in self.read()['alerts']))
        self.publish([self.entry()],revision=2)
        self.assertTrue(any(a['title']=='床位分配的正式排班来源已变化' for a in self.read()['alerts']))

    def test_after_midnight_uses_previous_day_night_shift(self):
        night=date(2026,10,26);self.publish([self.entry(day=night,shift='N')])
        bed=Bed.objects.create(group=self.group,number='1')
        Patient.objects.create(group=self.group,bed=bed,code='SYN-NIGHT',name='合成患者',reviewed_by=self.user)
        now=datetime(2026,10,27,6,tzinfo=ZoneInfo('Asia/Shanghai'))
        result=self.read(now)
        self.assertTrue(any(a['title']=='当前班次床位分配未发布' and a.get('date')=='2026-10-26' and a.get('shift')=='N' for a in result['alerts']))

    def test_missing_qualified_on_duty_nurse_is_reported_even_before_bed_publication(self):
        self.publish([self.entry()])
        bed=Bed.objects.create(group=self.group,number='1')
        patient=Patient.objects.create(group=self.group,bed=bed,code='SYN-QUAL-MISSING',name='合成患者',
            reviewed_by=self.user,treatments=['CRRT'])
        self.assertTrue(any(a['title']=='当前护理要求缺少合格当班护士' for a in self.read()['alerts']))
        patient.reviewed_by=None;patient.save(update_fields=['reviewed_by'])
        result=self.read()
        self.assertFalse(any(a['title']=='当前护理要求缺少合格当班护士' for a in result['alerts']))
        self.assertTrue(any(a['title']=='患者护理资料待核对' for a in result['alerts']))

    def test_validation_failure_keeps_other_panels_available(self):
        self.publish([self.entry()])
        with patch('scheduling.services.overview.schedule.validate',side_effect=ValueError('synthetic error')):
            with self.assertLogs('scheduling.services.overview',level='ERROR'):
                result=self.read()
        self.assertEqual(result['validation']['status'],'unavailable')
        self.assertIsNone(result['publication']['anomalies'])
        self.assertEqual(result['nurses_total'],1)
        self.assertTrue(any(a['title']=='正式排班校验暂不可用' for a in result['alerts']))

    def test_schedule_source_failure_does_not_erase_beds_or_claim_no_schedule(self):
        bed=Bed.objects.create(group=self.group,number='1')
        Patient.objects.create(group=self.group,bed=bed,code='SYN-UNAVAILABLE',name='合成患者',reviewed_by=self.user)
        with patch('scheduling.services.overview.schedule.current_publication',side_effect=ValueError('synthetic error')):
            with self.assertLogs('scheduling.services.overview',level='ERROR'):
                result=self.read()
        self.assertEqual((result['beds'],result['patients']),(1,1))
        self.assertEqual(result['data_quality']['schedule'],'unavailable')
        self.assertEqual(result['staffing']['status'],'unavailable')
        self.assertTrue(any(a['title']=='正式排班数据暂不可用' for a in result['alerts']))
        self.assertFalse(any(a['title']=='本周暂无有效正式排班' for a in result['alerts']))

    def test_group_scope_is_enforced(self):
        other=CareGroup.objects.create(name='总览联动另一组')
        Bed.objects.create(group=other,number='999',enabled=True)
        self.assertEqual(self.read()['beds'],0)
        self.assertEqual(self.client.get('/api/v1/overview/',{'group':other.id}).status_code,403)
