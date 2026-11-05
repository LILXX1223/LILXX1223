import copy
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.test import TestCase, TransactionTestCase, Client
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.utils import timezone
from rest_framework.test import APIClient
from scheduling.models import *
from scheduling.services import schedule, beds, ai, workload
from scheduling.services.common import BusinessError
from scripts.backup_database import backup_database

DAY=date(2026,10,22)


class WorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with patch('django.utils.timezone.localdate',return_value=DAY): call_command('seed_demo',verbosity=0)
        cls.group=CareGroup.objects.get(name='第3组')
        cls.manager=get_user_model().objects.get(username='group3')
        cls.nurse_user=get_user_model().objects.create_user('legacy-nurse',password='test-password',is_active=False)
        AccountProfile.objects.create(user=cls.nurse_user,role='disabled_nurse',group=cls.group,nurse=Nurse.objects.first())
        cls.head=get_user_model().objects.get(username='head_nurse')
        cls.draft=ScheduleDraft.objects.get()
        cls.bd=BedDraft.objects.get()

    def setUp(self):
        self.client=APIClient();self.client.force_authenticate(self.manager)
        # Historical extraction workflow tests use explicitly marked synthetic simulation.
        # Application default remains unconfigured; connection tests never enable this switch.
        simulation=patch.dict(os.environ,{'ICU_AI_ALLOW_MOCK':'true'})
        simulation.start();self.addCleanup(simulation.stop)
    def post(self,path,data=None): return self.client.post('/api/v1/'+path+'/',{'group':self.group.id,**(data or {})},format='json')
    def context(self):
        self.bd.refresh_from_db();self.group.refresh_from_db();self.bd.group=self.group
        return {'id':self.bd.id,'revision':self.bd.revision,'fingerprint':beds.fingerprint(self.bd)}
    def nurses(self): return list(Nurse.objects.filter(group=self.group).order_by('id'))

    def test_workspace_contains_database_records(self):
        r=self.client.get('/api/v1/workspace/',{'date':str(DAY),'week':'2026-10-19'})
        self.assertEqual(r.status_code,200);d=r.data['data'];self.assertEqual(len(d['nurses']),16)
        self.assertEqual(len(d['board']['beds']),8);self.assertEqual(sum(bool(b['patient']) for b in d['board']['beds']),7)
        self.assertEqual(d['user']['label'],'第3组账号')
    def test_unauthenticated_access_denied(self):
        self.client.force_authenticate(None);self.assertIn(self.client.get('/api/v1/workspace/').status_code,[401,403])
    def test_login_requires_csrf(self):
        c=Client(enforce_csrf_checks=True)
        r=c.post('/api/v1/auth/login/',{'username':'group3','password':'DemoICU!2026'},content_type='application/json')
        self.assertEqual(r.status_code,403)
        c.get('/api/v1/auth/csrf/');token=c.cookies['csrftoken'].value
        r=c.post('/api/v1/auth/login/',{'username':'group3','password':'DemoICU!2026'},content_type='application/json',HTTP_X_CSRFTOKEN=token)
        self.assertEqual(r.status_code,200)
    def test_cross_group_access_blocked(self):
        other=CareGroup.objects.create(name='第4组')
        r=self.client.get('/api/v1/workspace/',{'group':other.id});self.assertEqual(r.status_code,403)
        r=self.post('schedules/generate',{'group':other.id,'start_date':'2026-10-26'});self.assertEqual(r.status_code,403)
    def test_head_nurse_is_read_only(self):
        self.client.force_authenticate(self.head)
        self.assertEqual(self.client.get('/api/v1/workspace/').status_code,200)
        self.assertEqual(self.post('schedules/generate',{'start_date':'2026-10-26'}).status_code,403)
        self.assertEqual(self.post('beds/recommend',self.context()).status_code,403)
    def test_deactivated_personal_account_cannot_access_business_data(self):
        self.client.force_authenticate(self.nurse_user)
        self.assertEqual(self.client.get('/api/v1/workspace/',{'date':str(DAY),'week':'2026-10-19'}).status_code,403)
        self.assertEqual(self.post('nurses',{'name':'越权'}).status_code,403)
    def test_fixed_rotation_and_weekday_day_staff(self):
        first=self.nurses()[0];entries=[e['shift'] for e in self.draft.entries if e['nurse']==first.id]
        self.assertEqual(entries[:4],['A','N','POST','OFF'])
        fixed=self.nurses()[-1];self.assertEqual([e['shift'] for e in self.draft.entries if e['nurse']==fixed.id],['DAY']*5+['OFF']*2)
    def test_manual_night_rest_conflict_blocks_publish(self):
        n=self.nurses()[0]
        schedule.edit(self.draft,self.manager,{'revision':self.draft.revision,'nurse':n.id,'date':'2026-10-21','shift':'A','reason':'测试冲突'})
        report=schedule.validate(self.draft);self.assertFalse(report['valid']);self.assertTrue(any('N班后' in e['message'] for e in report['errors']))
        with self.assertRaises(BusinessError):schedule.publish(self.draft,self.manager,{'revision':self.draft.revision})
    def test_cross_week_night_conflict_detected(self):
        next_draft=schedule.generate(self.group,self.manager,'2026-10-26')
        n=next(n for n in self.nurses() if any(e['nurse']==n.id and e['date']=='2026-10-25' and e['shift']=='N' for e in self.draft.entries))
        schedule.edit(next_draft,self.manager,{'revision':next_draft.revision,'nurse':n.id,'date':'2026-10-26','shift':'A','reason':'跨周冲突'})
        self.assertTrue(any('N班后' in e['message'] for e in schedule.validate(next_draft)['errors']))
    def test_qualification_and_minimum_staff_enforced(self):
        n=self.nurses()[0];n.qualifications=['DAY'];n.save()
        report=schedule.validate(self.draft);self.assertTrue(any('资格' in e['message'] for e in report['errors']))
        self.group.rules['min_staff']['A']=99;self.group.save();self.draft.group=self.group
        self.assertTrue(any('人数不足' in e['message'] for e in schedule.validate(self.draft)['errors']))
    def test_stale_nurse_edit_returns_409(self):
        n=self.nurses()[0];data={'group':self.group.id,'revision':n.revision,'title':'护师（更新）'}
        self.assertEqual(self.client.patch(f'/api/v1/nurses/{n.id}/',data,format='json').status_code,200)
        self.assertEqual(self.client.patch(f'/api/v1/nurses/{n.id}/',data,format='json').status_code,409)
    def test_stale_schedule_revision_returns_409(self):
        n=self.nurses()[0];data={'id':self.draft.id,'revision':self.draft.revision,'nurse':n.id,'date':'2026-10-19','shift':'A','reason':'核对'}
        self.assertEqual(self.post('schedules/edit',data).status_code,200)
        self.assertEqual(self.post('schedules/edit',data).status_code,409)
    def test_schedule_publication_immutable_and_idempotent(self):
        p=SchedulePublication.objects.get();saved=copy.deepcopy(p.snapshot)
        same=schedule.publish(self.draft,self.manager,{'revision':self.draft.revision});self.assertEqual(same.pk,p.pk)
        n=self.nurses()[0];schedule.edit(self.draft,self.manager,{'revision':self.draft.revision,'nurse':n.id,'date':'2026-10-19','shift':'A','reason':'保留历史'})
        p.refresh_from_db();self.assertEqual(p.snapshot,saved)
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):p.save()
    def test_pending_request_not_applied_and_approved_applied(self):
        r=ShiftRequest.objects.get();new=schedule.generate(self.group,self.manager,'2026-10-26')
        self.assertFalse(any(e.get('request_id')==r.id for e in new.entries))
        self.post(f'requests/{r.id}/decision',{'revision':r.revision,'decision':'approved','note':'同意'})
        with self.assertRaises(BusinessError):
            schedule.generate(self.group,self.manager,'2026-10-26',revision=new.revision)
        new.refresh_from_db()
        new=schedule.generate(self.group,self.manager,'2026-10-26',revision=new.revision)
        applied=[e for e in new.entries if e.get('request_id')==r.id];self.assertEqual(len(applied),1);self.assertEqual(applied[0]['shift'],'DAY')
    def test_unapplied_approved_request_blocks_publication(self):
        r=ShiftRequest.objects.get();r.decision='approved';r.save()
        d=schedule.generate(self.group,self.manager,'2026-10-26',include_requests=False)
        self.assertTrue(any('尚未应用' in e['message'] for e in schedule.validate(d)['errors']))
    def test_manager_records_request_and_deactivated_nurse_cannot_approve(self):
        r=self.post('requests/create',{'nurse':self.nurses()[0].id,'date':'2026-10-27','kind':'leave','target_shift':'OFF','reason':'合成测试'})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.data['data']['submitted_by'],'group3')
        self.client.force_authenticate(self.nurse_user)
        self.assertEqual(self.post(f"requests/{r.data['data']['id']}/decision",{'revision':1,'decision':'approved'}).status_code,403)
    def test_custom_training_not_in_rotation_and_historical_hours_unchanged(self):
        before=SchedulePublication.objects.get().snapshot
        r=self.post('shifts',{'code':'TRAIN','name':'培训','start':'08:00','end':'12:00','hours':4,'working':True,'next_day':False,'effective_from':'2026-11-01','enabled':True})
        self.assertEqual(r.status_code,200);self.assertEqual(SchedulePublication.objects.get().snapshot,before)
        self.assertNotIn('TRAIN',schedule.CYCLE)
    def test_past_effective_shift_change_blocked(self):
        r=self.post('shifts',{'code':'A','name':'A班','start':'08:00','end':'17:00','hours':8,'working':True,'effective_from':'2026-10-19'})
        self.assertEqual(r.status_code,400)
    def test_bed_recommendation_is_preview_only(self):
        before=copy.deepcopy(self.bd.assignments);r=beds.recommend(self.bd)
        self.bd.refresh_from_db();self.assertEqual(self.bd.assignments,before);self.assertEqual(len(r['assignments']),7)
        self.assertTrue(beds.validate(self.bd,r['assignments'])['valid'])
    def test_insufficient_capacity_returns_no_solution(self):
        self.group.rules['max_beds']=2;self.group.save();self.bd.group=self.group
        with self.assertRaises(BusinessError) as cm:beds.recommend(self.bd)
        self.assertIn('无法生成合规方案',str(cm.exception))
    def test_special_treatment_requires_independent_certification(self):
        _,nurses=beds.on_duty(self.group,DAY,'A')
        for n in nurses:NurseSkill.objects.filter(nurse=n).update(independent=False,expires_on=None)
        with self.assertRaises(BusinessError):beds.recommend(self.bd)
    def test_expired_certification_rejected(self):
        _,nurses=beds.on_duty(self.group,DAY,'A')
        for n in nurses:NurseSkill.objects.filter(nurse=n).update(expires_on='2020-01-01')
        with self.assertRaises(BusinessError):beds.recommend(self.bd)
    def test_illegal_three_bed_combo_blocked(self):
        _,ns=beds.on_duty(self.group,DAY,'A');p=beds.patients(self.group)
        assignment={str(x.bed_id):ns[0].id for x in p[:3]}
        self.assertFalse(beds.validate(self.bd,assignment,partial=True)['valid'])
    def test_valid_three_light_and_two_light_one_medium(self):
        _,ns=beds.on_duty(self.group,DAY,'A');p=beds.patients(self.group)
        light=[x for x in p if x.load=='light'];medium=next(x for x in p if x.load=='medium')
        self.assertTrue(beds.validate(self.bd,{str(x.bed_id):ns[-1].id for x in light[:3]},partial=True)['valid'])
        self.assertTrue(beds.validate(self.bd,{str(x.bed_id):ns[-1].id for x in light[:2]+[medium]},partial=True)['valid'])
    def test_manual_assignment_changes_single_shared_mapping(self):
        data=self.context();data.update(assignments=self.bd.assignments,reason='人工核对')
        r=self.post('beds/adjust',data);self.assertEqual(r.status_code,200)
        self.bd.refresh_from_db();self.assertEqual(r.data['data']['assignments'],self.bd.assignments)
        self.assertEqual(self.post('beds/adjust',data).status_code,409)
    def test_duplicate_bed_publication_not_double_counted(self):
        before=workload.summary(self.group,DAY)['patients'];p=beds.publish(self.bd,self.manager,{**self.context(),'confirm_three':True})
        self.assertEqual(BedPublication.objects.count(),1);self.assertEqual(workload.summary(self.group,DAY)['patients'],before)
    def test_patient_change_invalidates_pending_preview(self):
        context=self.context();p=beds.patients(self.group)[0]
        self.post('beds/patient',{'bed_id':p.bed_id,'patient_id':p.id,'patient_revision':p.revision,'confirmed':True,
              'fields':{'name':p.name,'load':p.load,'synthetic':True}})
        self.assertEqual(self.post('beds/publish',{**context,'confirm_three':True}).status_code,409)
    def test_unreviewed_patient_blocks_allocation(self):
        p=beds.patients(self.group)[0];p.reviewed_by=None;p.save()
        self.assertFalse(beds.validate(self.bd)['valid'])
    def test_bed_cannot_be_disabled_while_occupied(self):
        self.group.refresh_from_db();r=self.post('beds/configure',{'revision':self.group.revision,'count':3});self.assertEqual(r.status_code,400)
    def test_extraction_does_not_write_before_review(self):
        before=Patient.objects.get(bed__number=3).name
        r=self.post('beds/extract',{'mode':'single','bed_number':3,'synthetic':True,'text':'姓名：测试合成；确定诊断：肺炎；护理负荷：较轻；当前机械通气。'})
        self.assertEqual(r.status_code,200);self.assertEqual(Patient.objects.get(bed__number=3).name,before)
        d=r.data['data'];result=self.post('beds/review',{'extraction':d['id'],'records':d['records'],'confirmed':True})
        self.assertEqual(result.status_code,200);self.assertEqual(Patient.objects.get(bed__number=3).name,'测试合成')
        self.assertEqual(self.post('beds/review',{'extraction':d['id'],'records':d['records'],'confirmed':True}).status_code,409)
    def test_changed_occupancy_blocks_review(self):
        r=self.post('beds/extract',{'mode':'single','bed_number':3,'synthetic':True,'text':'姓名：合成；护理负荷：较轻；诊断：肺炎；'})
        d=r.data['data'];p=Patient.objects.get(bed__number=3);p.revision+=1;p.save()
        self.assertEqual(self.post('beds/review',{'extraction':d['id'],'records':d['records'],'confirmed':True}).status_code,409)
    def test_batch_rejects_missing_duplicate_and_conflicting_beds(self):
        for text in ['患者甲肺炎；患者乙感染','3床：肺炎\n3床：感染']:
            with self.assertRaises(BusinessError):ai.split_records(text,'batch',None)
        with self.assertRaises(BusinessError):ai.split_records('4床：合成肺炎病例','single',3)
        self.assertEqual(len(ai.split_records('3床：肺炎\n4床：感染','batch',None)),2)
    def test_simulation_keeps_suspected_and_historical_separate(self):
        r=ai.simulated('姓名：合成；疑似诊断：ARDS；此前使用CRRT，现已停止；当前机械通气；')
        self.assertEqual(r['diagnoses'],[]);self.assertIn('ARDS',r['suspected']);self.assertNotIn('CRRT',r['treatments'])
        self.assertIn('机械通气',r['treatments']);self.assertTrue(r['historical'])
    def test_negative_and_planned_treatments_not_current(self):
        for text in ['当前未使用CRRT；','计划机械通气；','无ECMO治疗；']:
            self.assertEqual(ai.simulated(text)['treatments'],[])
    def test_external_ai_fails_closed(self):
        import httpx
        config={'configured':True,'hospital_authorized':True,'provider':'openai_compatible',
                'base_url':'https://api.example.com/v1','model':'test-model'}
        with patch('scheduling.services.ai.public_config',return_value=config), \
             patch('scheduling.services.ai.secret_value',return_value='test-key'), \
             patch('scheduling.services.ai_configuration.check_external_host',return_value='8.8.8.8'), \
             patch('scheduling.services.ai.httpx.Client.post',side_effect=httpx.TimeoutException('timeout')):
            with self.assertRaises(BusinessError) as cm:ai.extract('合成病历文本')
            self.assertEqual(cm.exception.status_code,502)
    def test_real_patient_requires_hospital_authorization(self):
        with patch.dict(os.environ,{'AI_HOSPITAL_AUTHORIZED':'false'}):
            with self.assertRaises(BusinessError):ai.extract('测试病历文本',synthetic=False)
    def test_workload_counts_seven_patients_and_no_empty_bed(self):
        result=workload.summary(self.group,DAY);self.assertEqual(result['patients'],7)
        self.assertEqual(sum(row['patients'] for row in result['rows']),7)
        self.assertIn('未连接考勤',result['source'])
    def test_hours_42_and_night_counted_once(self):
        n=self.nurses()[0]
        entries=[]
        for i,code,hours in [(0,'A',9),(1,'DAY',9),(3,'A',9),(4,'N',15)]:
            entries.append({'nurse':n.id,'date':str(date(2026,11,2)+timedelta(days=i)),'shift':code,'hours':hours,'working':True,'shift_name':code})
        d=ScheduleDraft.objects.create(group=self.group,start_date='2026-11-02',entries=[])
        SchedulePublication.objects.create(group=self.group,draft=d,draft_revision=1,start_date=d.start_date,published_by=self.manager,snapshot={'entries':entries})
        row=workload.summary(self.group,date(2026,11,2),nurse_id=n.id)['rows'][0]
        self.assertEqual(row['hours'],42);self.assertEqual(row['N'],1);self.assertEqual(sum(e['hours'] for e in row['daily']),42)
    def test_group_manager_cannot_read_or_write_api_key(self):
        r=self.client.get('/api/v1/workspace/');self.assertNotIn('API_KEY',str(r.data))
        self.group.refresh_from_db();r=self.client.patch('/api/v1/settings/',{'group':self.group.id,'revision':self.group.revision,'rules':{'AI_API_KEY':'bad'}},format='json')
        self.assertEqual(r.status_code,403)


class ConcurrentWriteTests(TransactionTestCase):
    def test_two_clients_same_revision_only_one_write_succeeds(self):
        group=CareGroup.objects.create(name='并发测试组');user=get_user_model().objects.create_user('concurrent',password='test')
        AccountProfile.objects.create(user=user,role='group_manager',group=group)
        nurse=Nurse.objects.create(group=group,name='并发合成护士',employee_id='CONCURRENT')
        barrier=Barrier(2)
        def worker(value):
            close_old_connections();c=APIClient();u=get_user_model().objects.get(pk=user.id);c.force_authenticate(u);barrier.wait()
            try:
                response=c.patch(f'/api/v1/nurses/{nurse.id}/',{'group':group.id,'revision':1,'title':value},format='json')
                return response.status_code
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(worker,['甲更新','乙更新']))
        nurse.refresh_from_db()
        if sorted(results)==[409,409]:
            # Shared in-memory SQLite may reject both concurrent writers before
            # either commits. This is a safe retry outcome, not a successful write.
            self.assertTrue(str(connection.settings_dict['NAME']).startswith('file:memorydb'))
            self.assertEqual(nurse.revision,1)
        else:
            self.assertEqual(sorted(results),[200,409]);self.assertEqual(nurse.revision,2)


class BackupTests(TestCase):
    def test_backup_and_isolated_restore_preserve_rows(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.sqlite3';target=Path(tmp)/'backup.sqlite3'
            with sqlite3.connect(source) as db:
                db.execute('CREATE TABLE evidence (id INTEGER PRIMARY KEY, label TEXT)');db.execute("INSERT INTO evidence(label) VALUES ('synthetic')")
            result=backup_database(source,target);self.assertEqual(result['integrity'],'ok');self.assertEqual(result['rows']['evidence'],1)
            with self.assertRaises(FileExistsError):backup_database(source,target)
