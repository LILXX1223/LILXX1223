from datetime import date
from unittest.mock import patch
from django.core.management import call_command
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from scheduling.models import AccountProfile, CareGroup, Nurse, NurseSkill, Skill, SkillDeletionSnapshot, Patient, BedDraft, BedPublication
from scheduling.services import beds, schedule


class SkillCatalogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with patch('django.utils.timezone.localdate',return_value=date(2026,10,22)):
            call_command('seed_demo',verbosity=0)
        cls.group=CareGroup.objects.get(name='第3组')
        cls.manager=get_user_model().objects.get(username='group3')
        cls.nurse_account=get_user_model().objects.create_user('legacy-nurse',password='test-password',is_active=False)
        AccountProfile.objects.create(user=cls.nurse_account,role='disabled_nurse',group=cls.group,nurse=Nurse.objects.first())
        cls.head=get_user_model().objects.get(username='head_nurse')

    def setUp(self):
        self.client=APIClient();self.client.force_authenticate(self.manager)

    def post(self,data):return self.client.post('/api/v1/skills/',{'group':self.group.id,**data},format='json')
    def patch(self,skill,data):
        return self.client.patch(f'/api/v1/skills/{skill.id}/',{'group':self.group.id,'revision':skill.revision,**data},format='json')
    def delete(self,skill):
        return self.client.delete(f'/api/v1/skills/{skill.id}/',{'group':self.group.id,'revision':skill.revision},format='json')

    def test_seed_catalog_and_current_nurse_counts(self):
        with patch('django.utils.timezone.localdate',return_value=date(2026,10,22)):
            response=self.client.get('/api/v1/skills/')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data['data']['active_nurses'],16)
        item=next(x for x in response.data['data']['items'] if x['code']=='CRRT')
        self.assertGreater(item['nurse_count'],0)
        self.assertEqual(item['nurse_count'],len(item['nurses']))
        self.assertEqual(item['coverage'],round(item['active_nurse_count']/16*100,1))
        self.assertEqual(self.client.get(f"/api/v1/skills/{item['id']}/nurses/").data['data'],item['nurses'])

    def test_group_manager_full_crud_and_history_snapshots(self):
        response=self.post({'code':'PRONE2','name':'人工技能','category':'呼吸支持','description':'演示',
            'enabled':True,'participates_bed':True,'participates_group':True})
        self.assertEqual(response.status_code,200);skill=Skill.objects.get(pk=response.data['data']['id'])
        nurse=Nurse.objects.first()
        response=self.client.patch(f'/api/v1/nurses/{nurse.id}/',{'group':self.group.id,'revision':nurse.revision,
            'skills':nurse.skills+['人工技能'],'skill_levels':{str(skill.id):'已培训'}},format='json')
        self.assertEqual(response.status_code,200)
        self.assertEqual(NurseSkill.objects.get(nurse=nurse,skill=skill).level,'已培训')
        response=self.patch(skill,{'name':'人工技能改名','description':'更名'})
        self.assertEqual(response.status_code,200);skill.refresh_from_db();nurse.refresh_from_db()
        self.assertIn('人工技能改名',nurse.skills);self.assertEqual(response.data['data']['nurse_count'],1)
        self.assertEqual(NurseSkill.objects.get(nurse=nurse,skill=skill).level,'已培训')
        response=self.delete(skill);self.assertEqual(response.status_code,200)
        self.assertFalse(Skill.objects.filter(pk=skill.id).exists())
        self.assertFalse(NurseSkill.objects.filter(skill_id=skill.id).exists())
        nurse.refresh_from_db();self.assertNotIn('人工技能改名',nurse.skills)
        snapshot=SkillDeletionSnapshot.objects.get(skill_id_at_deletion=skill.id,code='PRONE2')
        self.assertEqual(snapshot.associations[0]['nurse_id'],nurse.id)
        self.assertEqual(snapshot.associations[0]['level'],'已培训')
        self.assertFalse(snapshot.associations[0]['independent'])

    def test_permissions_group_scope_and_revision(self):
        skill=Skill.objects.get(group=self.group,code='CRRT')
        self.assertEqual(self.client.patch(f'/api/v1/skills/{skill.id}/',{'group':self.group.id,'revision':0,'name':'X'},format='json').status_code,409)
        other=CareGroup.objects.create(name='其他大组')
        self.assertEqual(self.client.get('/api/v1/skills/',{'group':other.id}).status_code,403)
        self.client.force_authenticate(self.head)
        self.assertEqual(self.client.get('/api/v1/skills/',{'group':self.group.id}).status_code,200)
        self.assertEqual(self.patch(skill,{'name':'X'}).status_code,403)
        self.client.force_authenticate(self.nurse_account)
        self.assertEqual(self.client.get('/api/v1/skills/').status_code,403)

    def test_renamed_builtin_qualification_keeps_stable_identity(self):
        skill=Skill.objects.get(group=self.group,code='CRRT');nurse=Nurse.objects.first();patient=Patient.objects.get(bed__number=1)
        self.assertTrue(beds.qualified(nurse,patient,date(2026,10,22),self.group.rules))
        self.assertEqual(self.patch(skill,{'name':'连续肾脏替代治疗'}).status_code,200)
        nurse.refresh_from_db()
        self.assertTrue(beds.qualified(nurse,patient,date(2026,10,22),self.group.rules))
        self.assertIn('连续肾脏替代治疗',[x['name'] for x in nurse.certifications])

    def test_disabled_or_deleted_special_skill_cannot_bypass_bed_qualification(self):
        nurse=Nurse.objects.first();patient=Patient.objects.get(bed__number=1)
        skill=Skill.objects.get(group=self.group,code='CRRT');pub=BedPublication.objects.get();original=pub.snapshot
        self.assertTrue(beds.qualified(nurse,patient,date(2026,10,22),self.group.rules))
        self.assertEqual(self.patch(skill,{'enabled':False}).status_code,200)
        nurse.refresh_from_db();self.assertFalse(beds.qualified(nurse,patient,date(2026,10,22),self.group.rules))
        skill.refresh_from_db();self.assertEqual(self.delete(skill).status_code,200)
        snapshot=SkillDeletionSnapshot.objects.get(skill_id_at_deletion=skill.id)
        self.assertTrue(any(x['nurse_id']==nurse.id and x['independent'] and x['expires_on']=='2030-12-31' for x in snapshot.associations))
        nurse.refresh_from_db();self.assertFalse(beds.qualified(nurse,patient,date(2026,10,22),self.group.rules))
        pub.refresh_from_db();self.assertEqual(pub.snapshot,original)
        self.assertTrue(all('nurse_skill_snapshot' in row for row in original['assignments']))

    def test_treatment_name_matching_is_case_insensitive(self):
        nurse=Nurse.objects.first();patient=Patient.objects.get(bed__number=1)
        patient.treatments=['crrt']
        self.assertTrue(beds.qualified(nurse,patient,date(2026,10,22),self.group.rules))
        skill=Skill.objects.get(group=self.group,code='CRRT')
        self.patch(skill,{'enabled':False})
        self.assertFalse(beds.qualified(nurse,patient,date(2026,10,22),self.group.rules))

    def test_disabled_skill_cannot_be_newly_assigned(self):
        skill=Skill.objects.get(group=self.group,code='ECMO')
        self.assertEqual(self.patch(skill,{'enabled':False}).status_code,200)
        nurse=Nurse.objects.first();nurse.refresh_from_db()
        response=self.client.patch(f'/api/v1/nurses/{nurse.id}/',{'group':self.group.id,'revision':nurse.revision,
            'skills':nurse.skills+['ECMO']},format='json')
        self.assertEqual(response.status_code,400)

    def test_disabled_skill_certificate_cannot_be_added_or_extended(self):
        skill=Skill.objects.get(group=self.group,code='AIRWAY')
        self.assertEqual(self.patch(skill,{'enabled':False}).status_code,200)
        nurse=Nurse.objects.first();nurse.refresh_from_db()
        modified=[{**cert,'expires_on':'2040-01-01'} if cert['name']=='气道护理' else cert for cert in nurse.certifications]
        response=self.client.patch(f'/api/v1/nurses/{nurse.id}/',{'group':self.group.id,'revision':nurse.revision,
            'certifications':modified},format='json')
        self.assertEqual(response.status_code,400)
        nurse.refresh_from_db();self.assertNotIn('2040-01-01',[x['expires_on'] for x in nurse.certifications])

    def test_unassigned_nurse_draft_fails_closed_and_new_fields_roundtrip(self):
        nurse=Nurse.objects.first()
        response=self.client.patch(f'/api/v1/nurses/{nurse.id}/',{'group':self.group.id,'revision':nurse.revision,
            'subgroup':None,'assessment_level':3,'teaching_collaboration':'参与带教，团队协作待评估'},format='json')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data['data']['assessment_level'],3)
        nurse.refresh_from_db();self.assertIsNone(nurse.subgroup)
        draft=schedule.generate(self.group,self.manager,'2026-11-02')
        report=schedule.validate(draft)
        self.assertFalse(report['valid'])
        self.assertTrue(any('尚未分配轮转小组' in e['message'] for e in report['errors']))
