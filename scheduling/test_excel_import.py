import io
import importlib.util
import json
from unittest import skipUnless
from unittest.mock import patch
from datetime import date
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import F
from django.test import TestCase
from openpyxl import Workbook
from rest_framework.test import APIClient
from scheduling.models import AccountProfile, CareGroup, Nurse, NurseImportBatch, NurseSkill, Skill


def workbook(rows,second=False):
    output=io.BytesIO();book=Workbook();sheet=book.active;sheet.title='护士资料'
    for row in rows:sheet.append(row)
    if second:
        extra=book.create_sheet('其他人员');extra.append(['姓名','工号']);extra.append(['合成第二表','TEST-2'])
    book.save(output);return SimpleUploadedFile('合成人员.xlsx',output.getvalue(),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


class NurseExcelImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users=get_user_model();cls.manager=users.objects.create_user('excel-manager',password='test')
        cls.head=users.objects.create_user('excel-head',password='test')
        cls.group=CareGroup.objects.create(name='导入测试组');cls.other=CareGroup.objects.create(name='其他测试组')
        AccountProfile.objects.create(user=cls.manager,role='group_manager',group=cls.group)
        AccountProfile.objects.create(user=cls.head,role='head_nurse')
        cls.skill=Skill.objects.create(group=cls.group,code='CRRT',name='CRRT',enabled=True,match_terms=['CRRT'])
        cls.old=Nurse.objects.create(group=cls.group,name='合成张护士',employee_id='T001',phone='13800001234',
            qualifications=['A','N'],skills=['CRRT'],years=4,subgroup=1)
        NurseSkill.objects.create(nurse=cls.old,skill=cls.skill)

    def setUp(self):
        self.client=APIClient();self.client.force_authenticate(self.manager)

    def upload(self,rows,second=False):
        return self.client.post('/api/v1/imports/excel/',{'group':self.group.pk,'file':workbook(rows,second)},format='multipart')

    def edit(self,batch,**values):
        return self.client.patch(f"/api/v1/imports/excel/{batch['id']}/",{'group':self.group.pk,'revision':batch['revision'],**values},format='json')

    def confirm(self,batch,**values):
        return self.client.post(f"/api/v1/imports/excel/{batch['id']}/confirm/",{'group':self.group.pk,'revision':batch['revision'],'confirmed':True,**values},format='json')

    def test_preview_match_by_employee_then_atomic_confirm_preserves_absent_fields(self):
        response=self.upload([['姓名','工号','层级','年资','技能','带教与协作能力','病情评估能力能级'],
            ['合成张护士','T001','N2','6年','CRRT','参与带教，协作待评价','3档'],['合成新护士','T002','N1','2','CRRT','','2']])
        self.assertEqual(response.status_code,200);batch=response.data['data']
        self.assertEqual(batch['identification_mode'],'local_rules')
        self.assertEqual([x['action'] for x in batch['records']],['update','new'])
        self.assertEqual(batch['records'][0]['expected_revision'],self.old.revision)
        self.assertEqual(Nurse.objects.filter(group=self.group).count(),1)
        result=self.confirm(batch);self.assertEqual(result.status_code,200,result.data)
        self.old.refresh_from_db();self.assertEqual(self.old.phone,'13800001234')
        self.assertEqual(self.old.years,6);self.assertEqual(self.old.assessment_level,3)
        self.assertEqual(self.old.teaching_collaboration,'参与带教，协作待评价')
        self.assertEqual(Nurse.objects.filter(group=self.group).count(),2)
        self.assertTrue(NurseSkill.objects.filter(nurse__employee_id='T002',skill=self.skill).exists())
        self.assertIsNone(Nurse.objects.get(employee_id='T002').subgroup)
        self.assertEqual(self.confirm(batch).status_code,409)

    def test_name_alone_never_matches_automatically_and_demo_id_requires_approval(self):
        batch=self.upload([['姓名','年资'],['合成张护士','7'],['无工号新人','2']]).data['data']
        self.assertEqual([r['action'] for r in batch['records']],['review','review'])
        self.assertEqual(batch['records'][0]['candidates'][0]['id'],self.old.id)
        edited=self.edit(batch,records=[{'row_number':2,'action':'skip'},{'row_number':3,'action':'new'}]).data['data']
        self.assertEqual(self.confirm(edited).status_code,422)
        saved=self.confirm(edited,allow_demo_ids=True);self.assertEqual(saved.status_code,200,saved.data)
        result=saved.data['data']['result'][1];self.assertTrue(result['demo_id']);self.assertTrue(result['employee_id'].startswith('DEMO'))
        self.old.refresh_from_db();self.assertEqual(self.old.years,4)

    def test_name_candidate_cannot_replace_existing_employee_id(self):
        batch=self.upload([['姓名','工号'],['合成张护士','DEMO999']]).data['data']
        self.assertEqual(batch['records'][0]['action'],'review')
        batch=self.edit(batch,records=[{'row_number':2,'action':'update','target_id':self.old.id,
            'expected_revision':self.old.revision}]).data['data']
        self.assertEqual(self.confirm(batch,allow_demo_ids=True).status_code,422)
        self.old.refresh_from_db();self.assertEqual(self.old.employee_id,'T001')

    def test_manual_mapping_sheet_and_stale_revision(self):
        batch=self.upload([['奇特名称','编号'],['合成新护士','TEST-1']],True).data['data']
        self.assertEqual(batch['sheets'][1]['name'],'其他人员')
        changed=self.edit(batch,mapping={'奇特名称':'name','编号':'employee_id'}).data['data']
        self.assertEqual(changed['records'][0]['fields']['name'],'合成新护士')
        self.assertEqual(self.edit(batch,selected_sheet=1).status_code,409)
        second=self.edit(changed,selected_sheet=1).data['data']
        self.assertEqual(second['records'][0]['fields']['employee_id'],'TEST-2')

    def test_unknown_skill_or_duplicate_employee_blocks_entire_batch(self):
        batch=self.upload([['姓名','工号','技能'],['合成张护士','T001','未知技能'],['合成重复','T001','CRRT']]).data['data']
        self.assertIn('skills',batch['records'][0]['issues'])
        response=self.confirm(batch);self.assertEqual(response.status_code,422)
        self.old.refresh_from_db();self.assertEqual(self.old.years,4)
        self.assertEqual(Nurse.objects.filter(group=self.group).count(),1)
        stored=NurseImportBatch.objects.get(pk=batch['id'])
        self.assertEqual(stored.status,'preview');self.assertEqual(stored.result[0]['status'],'error')
        self.assertTrue(stored.result[0]['messages'])

    @skipUnless(importlib.util.find_spec('xlwt'), 'xlwt only needed to construct an .xls test fixture')
    def test_legacy_xls_file_and_date_cell(self):
        import xlwt
        from datetime import datetime
        book=xlwt.Workbook();sheet=book.add_sheet('护士')
        for col,value in enumerate(['姓名','工号','生效日期']):sheet.write(0,col,value)
        sheet.write(1,0,'旧格式合成护士');sheet.write(1,1,'OLD001')
        sheet.write(1,2,datetime(2026,10,1),xlwt.easyxf(num_format_str='YYYY-MM-DD'))
        output=io.BytesIO();book.save(output)
        upload=SimpleUploadedFile('旧格式.xls',output.getvalue(),content_type='application/vnd.ms-excel')
        response=self.client.post('/api/v1/imports/excel/',{'group':self.group.pk,'file':upload},format='multipart')
        self.assertEqual(response.status_code,200,response.data)
        batch=response.data['data'];self.assertEqual(batch['records'][0]['fields']['effective_from'],'2026-10-01')
        self.assertEqual(self.confirm(batch).status_code,200)
        self.assertEqual(Nurse.objects.get(employee_id='OLD001').effective_from,date(2026,10,1))

    def test_explicit_independent_qualification_requires_validity_and_confirmation(self):
        batch=self.upload([['姓名','工号','独立护理资格'],['合成张护士','T001','CRRT']]).data['data']
        row=batch['records'][0]
        self.assertIn('certifications',row['issues']);self.assertNotIn('certifications',row['fields'])
        fields={**row['fields'],'certifications':[{'name':'CRRT','expires_on':'2030-12-31'}]}
        edited=self.edit(batch,records=[{'row_number':2,'fields':fields}]).data['data']
        self.assertEqual(self.confirm(edited).status_code,422)
        approved=self.edit(edited,records=[{'row_number':2,'confirm_certifications':True}]).data['data']
        self.assertEqual(self.confirm(approved).status_code,200)
        self.assertTrue(NurseSkill.objects.get(nurse=self.old,skill=self.skill).independent)

    def test_stale_existing_nurse_blocks_without_partial_writes(self):
        batch=self.upload([['姓名','工号','年资'],['合成张护士','T001','8'],['合成新人','T002','1']]).data['data']
        Nurse.objects.filter(pk=self.old.pk).update(revision=F('revision')+1)
        result=self.confirm(batch);self.assertEqual(result.status_code,422)
        self.assertFalse(Nurse.objects.filter(employee_id='T002').exists())

    def test_upload_validation_and_group_permissions(self):
        bad=SimpleUploadedFile('bad.txt',b'fake')
        self.assertEqual(self.client.post('/api/v1/imports/excel/',{'group':self.group.id,'file':bad},format='multipart').status_code,400)
        bad=SimpleUploadedFile('bad.xlsx',b'not a zip')
        self.assertEqual(self.client.post('/api/v1/imports/excel/',{'group':self.group.id,'file':bad},format='multipart').status_code,400)
        self.assertEqual(self.client.post('/api/v1/imports/excel/',{'group':self.other.id,'file':workbook([['姓名'],['X']])},format='multipart').status_code,403)
        self.client.force_authenticate(self.head)
        self.assertEqual(self.client.post('/api/v1/imports/excel/',{'group':self.group.id,'file':workbook([['姓名'],['X']])},format='multipart').status_code,403)

    def test_sheet_row_column_and_header_limits(self):
        duplicated=self.upload([['姓名','姓名'],['甲','乙']])
        self.assertEqual(duplicated.status_code,400)
        too_many_columns=self.upload([[f'列{i}' for i in range(61)],['合成']])
        self.assertEqual(too_many_columns.status_code,400)
        too_many_rows=self.upload([['姓名']]+[[f'合成{i}'] for i in range(501)])
        self.assertEqual(too_many_rows.status_code,400)
        self.assertEqual(NurseImportBatch.objects.count(),0)

    def test_ai_suggestion_uses_real_service_contract_never_sends_rows(self):
        batch=self.upload([['姓名','不规范年资'],['隐私测试姓名','5']]).data['data']
        url=f"/api/v1/imports/excel/{batch['id']}/ai-suggest/"
        payload={'group':self.group.id,'revision':batch['revision'],'synthetic':True}
        self.assertEqual(self.client.post(url,payload,format='json').status_code,503)
        config={'configured':True,'model':'synthetic-model','provider':'deepseek','base_url':'https://example.com/v1'}
        suggestion=json.dumps({'column_map':{'姓名':'name','不规范年资':'years'}})
        with patch('scheduling.services.excel_import.public_config',return_value=config),\
             patch('scheduling.services.excel_import.secret_value',return_value='dummy'),\
             patch('scheduling.services.excel_import.chat_completion',return_value=suggestion) as call:
            result=self.client.post(url,payload,format='json')
        self.assertEqual(result.status_code,200);self.assertEqual(result.data['data']['ai_status'],'suggested')
        self.assertNotIn('隐私测试姓名',str(call.call_args))
        self.assertEqual(result.data['data']['mapping']['不规范年资'],'')

    def test_real_data_cannot_be_sent_to_external_ai_without_authorization(self):
        batch=self.upload([['姓名'],['合成资料']]).data['data']
        with patch('scheduling.services.excel_import.hospital_authorized',return_value=False),\
             patch('scheduling.services.excel_import.chat_completion') as call:
            result=self.client.post(f"/api/v1/imports/excel/{batch['id']}/ai-suggest/",
                {'group':self.group.id,'revision':batch['revision'],'synthetic':False},format='json')
        self.assertEqual(result.status_code,403);call.assert_not_called()
