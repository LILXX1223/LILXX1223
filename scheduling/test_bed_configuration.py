from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
from datetime import date
import os
from django.core.management import call_command
from django.db import close_old_connections
from threading import Barrier

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from scheduling.models import (AccountProfile, AuditLog, Bed, BedDraft, BedGroupConfiguration,
                               BedPublication, CareGroup, Patient)
from scheduling.services import ai, beds
from scheduling.services.common import BusinessError


class BedConfigurationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.group = CareGroup.objects.create(name='配置测试组')
        cls.other = CareGroup.objects.create(name='配置测试另一组')
        cls.manager = get_user_model().objects.create_user('bed-manager')
        cls.outsider = get_user_model().objects.create_user('other-manager')
        cls.head = get_user_model().objects.create_user('bed-head')
        AccountProfile.objects.create(user=cls.manager, group=cls.group, role='group_manager')
        AccountProfile.objects.create(user=cls.outsider, group=cls.other, role='group_manager')
        AccountProfile.objects.create(user=cls.head, group=cls.group, role='head_nurse')
        cls.config = BedGroupConfiguration.objects.create(group=cls.group, start_number=1)
        BedGroupConfiguration.objects.create(group=cls.other, start_number=54)
        Bed.objects.bulk_create([Bed(group=cls.group, number=str(n)) for n in range(1,54)])
        Bed.objects.create(group=cls.other, number='54')
        for number in ('1','2'):
            Patient.objects.create(group=cls.group, bed=Bed.objects.get(group=cls.group,number=number),
                code='SYN-BED-CONFIG-'+number, name='合成患者', reviewed_by=cls.manager)
        past = timezone.localdate()-timedelta(days=10)
        cls.old_draft = BedDraft.objects.create(group=cls.group,date=past,shift='A')
        cls.old_pub = BedPublication.objects.create(group=cls.group,draft=cls.old_draft,draft_revision=1,
            date=past,shift='A',snapshot={'assignments':[{'bed':Bed.objects.get(group=cls.group,number='4').id,
                'bed_number':4,'nurse':123,'patient':456}]},published_by=cls.manager)

    def setUp(self):
        self.client=APIClient(); self.client.force_authenticate(self.manager)

    def payload(self, numbers=None):
        self.group.refresh_from_db(); self.config.refresh_from_db()
        rows = list(Bed.objects.filter(group=self.group, configured=True))
        if numbers is not None:
            wanted={str(x).upper() for x in numbers}; rows=[b for b in rows if str(b.number).upper() in wanted]
        return {'group':self.group.id,'revision':self.group.revision,'config_version':self.config.version,
                'start_number':1,'beds':[{'id':b.id,'number':b.number,'visible':b.visible,'enabled':b.enabled} for b in rows]}

    def post(self, action, payload):
        return self.client.post('/api/v1/beds/'+action+'/',payload,format='json')

    def test_noncontinuous_custom_beds_are_saved_without_reusing_or_removing_ids(self):
        existing_ids=dict(Bed.objects.filter(group=self.group).values_list('number','id'))
        original=deepcopy(self.old_pub.snapshot)
        data=self.payload([1,2,5,8]); data['beds'].append({'id':None,'number':'101a','visible':True,'enabled':True})
        data['beds'][2]['visible']=False
        preview=self.post('configure-preview',data)
        self.assertEqual(preview.status_code,200);self.assertTrue(preview.data['data']['can_save'])
        self.assertEqual(len(preview.data['data']['removed']),49)
        response=self.post('configure',data);self.assertEqual(response.status_code,200)
        self.assertEqual(response.data['data']['version'],2)
        self.assertEqual(response.data['data']['configured_count'],5)
        self.assertEqual(response.data['data']['visible_count'],4)
        self.assertEqual(response.data['data']['open_count'],54)
        self.assertEqual(dict(Bed.objects.filter(group=self.group).exclude(number='101A').values_list('number','id')),existing_ids)
        self.assertEqual(Patient.objects.filter(group=self.group,bed__isnull=False).count(),2)
        self.old_pub.refresh_from_db();self.assertEqual(self.old_pub.snapshot,original)
        self.assertTrue(Bed.objects.get(group=self.group,number='4').enabled)
        self.assertFalse(Bed.objects.get(group=self.group,number='4').configured)
        self.assertTrue(AuditLog.objects.filter(group=self.group,actor=self.manager,action='修改床位配置').exists())
        workspace=self.client.get('/api/v1/workspace/',{'group':self.group.id}).data['data']
        self.assertEqual([b['number'] for b in workspace['board']['beds']],[1,2,8,'101A'])
        self.assertEqual(workspace['board']['statistics'],{'configured':5,'visible':4,'open':54,'occupied':2,'empty':52,'open_not_shown':50})
        self.assertEqual(workspace['bed_configuration']['start_number'],1)

    def test_occupied_bed_cannot_be_hidden_closed_or_removed(self):
        for kind in ('hidden','closed','removed'):
            data=self.payload()
            row=next(row for row in data['beds'] if str(row['number'])=='1')
            if kind=='hidden':row['visible']=False
            elif kind=='closed':row['enabled']=False
            else:data['beds'].remove(row)
            result=self.post('configure-preview',data)
            self.assertEqual(result.status_code,200);self.assertFalse(result.data['data']['can_save'])
            self.assertEqual(self.post('configure',data).status_code,422)
            self.group.refresh_from_db();self.assertEqual(self.group.revision,data['revision'])
            self.assertTrue(Patient.objects.filter(group=self.group,bed__number='1').exists())

    def test_future_assignment_is_protected_but_old_publication_is_retained(self):
        bed=Bed.objects.get(group=self.group,number='3')
        BedDraft.objects.create(group=self.group,date=timezone.localdate()+timedelta(days=1),shift='A',assignments={str(bed.id):10})
        data=self.payload();next(x for x in data['beds'] if x['id']==bed.id)['visible']=False
        self.assertFalse(self.post('configure-preview',data).data['data']['can_save'])
        self.assertEqual(self.post('configure',data).status_code,422)
        data=self.payload();data['beds']=[row for row in data['beds'] if str(row['number'])!='4']
        self.assertEqual(self.post('configure',data).status_code,200)
        self.assertTrue(BedPublication.objects.filter(pk=self.old_pub.pk).exists())

    def test_cross_group_conflict_and_scope_enforced(self):
        data=self.payload();data['beds'].append({'id':None,'number':54,'visible':True,'enabled':False})
        self.assertEqual(self.post('configure',data).status_code,409)
        data['group']=self.other.id
        self.assertEqual(self.post('configure-preview',data).status_code,403)
        self.client.force_authenticate(self.head)
        self.assertEqual(self.post('configure',data).status_code,403)
        self.client.force_authenticate(None)
        self.assertIn(self.post('configure',data).status_code,[401,403])
        # The same label in a distinct ward is not a physical conflict.
        self.other.rules={**self.other.rules,'ward_code':'ICU-OTHER'};self.other.save()
        data=self.payload();data['beds'].append({'id':None,'number':54,'visible':True,'enabled':False})
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.post('configure',data).status_code,200)

    def test_group_settings_and_workspaces_are_independent(self):
        self.client.force_authenticate(self.outsider)
        other_config=BedGroupConfiguration.objects.get(group=self.other)
        existing=Bed.objects.get(group=self.other,number='54')
        data={'group':self.other.id,'revision':self.other.revision,'config_version':other_config.version,
              'start_number':101,'beds':[{'id':existing.id,'number':54,'visible':True,'enabled':True},
                    {'id':None,'number':101,'visible':True,'enabled':True},
                    {'id':None,'number':103,'visible':False,'enabled':True}]}
        self.assertEqual(self.post('configure',data).status_code,200)
        other=self.client.get('/api/v1/workspace/',{'group':self.other.id}).data['data']
        self.assertEqual(other['bed_configuration']['start_number'],101)
        self.assertEqual(other['bed_configuration']['configured_count'],3)
        self.assertEqual([b['number'] for b in other['board']['beds']],[54,101])
        self.assertEqual(other['board']['statistics']['open'],3)
        self.assertEqual(self.client.get('/api/v1/workspace/',{'group':self.group.id}).status_code,403)
        self.client.force_authenticate(self.manager)
        first=self.client.get('/api/v1/workspace/',{'group':self.group.id}).data['data']
        self.assertEqual(first['bed_configuration']['start_number'],1)
        self.assertEqual(first['bed_configuration']['configured_count'],53)
        self.assertEqual([b['number'] for b in first['board']['beds']],list(range(1,54)))

    def test_rename_duplicates_and_stale_writes_are_rejected(self):
        data=self.payload();data['beds'][0]['number']='101A'
        self.assertEqual(self.post('configure',data).status_code,400)
        data=self.payload();data['beds'].append({'id':None,'number':'01','visible':True,'enabled':False})
        self.assertEqual(self.post('configure',data).status_code,400)
        data=self.payload();self.assertEqual(self.post('configure',data).status_code,200)
        self.assertEqual(self.post('configure',data).status_code,409)

    def test_hidden_legacy_occupied_bed_remains_visible_and_blocks_solver(self):
        Bed.objects.filter(group=self.group,number='1').update(visible=False,configured=False)
        response=self.client.get('/api/v1/workspace/',{'group':self.group.id}).data['data']
        self.assertTrue(any(b['number']==1 and b['patient'] for b in response['board']['beds']))
        report=beds.validate(BedDraft(group=self.group,date=timezone.localdate(),shift='A'),{},partial=True)
        self.assertTrue(any('未显示或不在配置范围' in issue['message'] for issue in report['errors']))
        self.assertEqual(response['board']['statistics']['occupied'],2)

    def test_custom_bed_number_works_with_identification_and_review_boundary(self):
        self.assertEqual(ai.split_records('101A床：合成病历内容', 'single', '101a')[0][0],'101A')
        self.assertEqual([number for number,_ in ai.split_records('101A床：病例甲\n105床：病例乙','batch',None)],['101A',105])
        with self.assertRaises(BusinessError):ai.split_records('101A床：合成病例', 'single', 101)
        with self.assertRaises(BusinessError):ai.split_records('合成病例资料', 'single', True)

    def test_custom_bed_extraction_waits_for_review_and_uses_stable_id(self):
        data=self.payload();data['beds'].append({'id':None,'number':'101A','visible':True,'enabled':True})
        self.assertEqual(self.post('configure',data).status_code,200)
        bed=Bed.objects.get(group=self.group,number='101A')
        with patch.dict(os.environ,{'ICU_AI_ALLOW_MOCK':'true'}):
            plan=self.post('extract-prepare',{'group':self.group.id,'mode':'single','bed_number':'101A',
                'synthetic':True,'text':'101A床：姓名：合成患者；确定诊断：肺炎；护理负荷：较轻；'})
            self.assertEqual(plan.status_code,200)
            item=plan.data['data']['records'][0]
            result=self.post('extract-prepared',{'group':self.group.id,'token':item['token'],'text':item['text']})
            self.assertEqual(result.status_code,200)
            self.assertFalse(Patient.objects.filter(bed=bed).exists())
            extraction=result.data['data']
            reviewed=self.post('review',{'group':self.group.id,'extraction':extraction['id'],
                'records':extraction['records'],'confirmed':True})
            self.assertEqual(reviewed.status_code,200)
        self.assertTrue(Patient.objects.filter(group=self.group,bed_id=bed.id).exists())

    def test_configuration_change_invalidates_bed_drafts_not_formal_records(self):
        draft=BedDraft.objects.create(group=self.group,date=timezone.localdate()+timedelta(days=2),shift='N')
        old_revision=draft.revision;data=self.payload()
        next(x for x in data['beds'] if str(x['number'])=='53')['visible']=False
        self.assertEqual(self.post('configure',data).status_code,200)
        draft.refresh_from_db();self.assertEqual(draft.revision,old_revision+1)
        self.assertTrue(BedPublication.objects.filter(pk=self.old_pub.pk).exists())


class CustomBedSolverTests(TestCase):
    def test_ortools_and_publication_use_custom_number_without_changing_history(self):
        day=date(2026,10,22)
        with patch('django.utils.timezone.localdate',return_value=day):
            call_command('seed_demo',verbosity=0)
        group=CareGroup.objects.get(name='第3组')
        user=get_user_model().objects.get(username='group3')
        draft=BedDraft.objects.get(group=group,date=day,shift='A')
        previous=BedPublication.objects.first();previous_snapshot=deepcopy(previous.snapshot)
        patient=Patient.objects.filter(group=group,bed__isnull=False).first()
        new_bed=Bed.objects.create(group=group,number='101A',enabled=True,configured=True,visible=True)
        patient.bed=new_bed;patient.save(update_fields=['bed'])
        result=beds.recommend(draft)
        self.assertIn(str(new_bed.id),result['assignments'])
        self.assertIn(result['solver']['status'],['optimal','feasible'])
        previous.refresh_from_db();self.assertEqual(previous.snapshot,previous_snapshot)


class BedConfigurationConcurrencyTests(TransactionTestCase):
    def test_competing_groups_cannot_claim_same_physical_bed(self):
        groups=[CareGroup.objects.create(name=f'并发床位组{i}') for i in (1,2)]
        users=[get_user_model().objects.create_user(f'bed-concurrent-{i}') for i in (1,2)]
        for group,user in zip(groups,users):AccountProfile.objects.create(user=user,group=group,role='group_manager')
        barrier=Barrier(2)
        def create(index):
            close_old_connections();client=APIClient()
            client.force_authenticate(get_user_model().objects.get(pk=users[index].pk));barrier.wait()
            try:
                return client.post('/api/v1/beds/configure/',{'group':groups[index].id,'revision':1,
                    'config_version':0,'start_number':101,'beds':[{'id':None,'number':101,
                    'visible':True,'enabled':False}]},format='json').status_code
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:statuses=list(pool.map(create,[0,1]))
        self.assertTrue(all(status in (200,409) for status in statuses))
        self.assertLessEqual(statuses.count(200),1)
        self.assertLessEqual(Bed.objects.filter(number='101').count(),1)
        if not Bed.objects.filter(number='101').exists():
            client=APIClient();client.force_authenticate(users[0])
            self.assertEqual(client.post('/api/v1/beds/configure/',{'group':groups[0].id,'revision':1,
                'config_version':0,'start_number':101,'beds':[{'id':None,'number':101,
                'visible':True,'enabled':False}]},format='json').status_code,200)
        owner=Bed.objects.get(number='101').group_id
        loser=next((group,user) for group,user in zip(groups,users) if group.id!=owner)
        client=APIClient();client.force_authenticate(loser[1])
        self.assertEqual(client.post('/api/v1/beds/configure/',{'group':loser[0].id,'revision':1,
            'config_version':0,'start_number':101,'beds':[{'id':None,'number':101,
            'visible':True,'enabled':False}]},format='json').status_code,409)
