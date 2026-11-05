import os
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from scheduling.models import *
from scheduling.services import schedule, beds
from scheduling.services.skills import ensure_catalog, sync_nurse_skills


class Command(BaseCommand):
    help='增量创建合成演示数据，不清表、不重置密码、不覆盖已有业务记录。'
    @transaction.atomic
    def handle(self,*args,**options):
        today=timezone.localdate(); monday=today-timedelta(days=today.weekday())
        group,created=CareGroup.objects.get_or_create(name='第3组')
        if not created and Nurse.objects.filter(group=group).exists():
            self.stdout.write('已有演示组及人员，保留现有数据。'); return
        rules=default_rules(); rules['anchor']=str(monday); group.rules=rules; group.save()
        ensure_catalog(group)
        definitions=[('A','A班','08:00','17:00',False,9,True,'#fff0df'),('DAY','白班','08:00','17:00',False,9,True,'#e6f4ff'),
            ('N','N班','17:00','08:00',True,15,True,'#f0e7ff'),('POST','夜下休','08:00','08:00',False,0,False,'#e3faeb'),
            ('OFF','休','00:00','00:00',False,0,False,'#f0f1f4'),('TRAIN','培训','08:00','12:00',False,4,True,'#ffe7f1')]
        for code,name,start,end,nd,hours,working,color in definitions:
            ShiftType.objects.get_or_create(group=group,code=code,effective_from='2020-01-01',defaults=dict(name=name,start=start,end=end,next_day=nd,hours=hours,working=working,color=color))
        names=['张雪','陈晨','李娜','王芳','刘洋','孙悦','周琳','吴倩','郑雨','赵敏','周悦','孙雨','黄敏','高阳','林静','王菲']
        nurses=[]
        for i,name in enumerate(names):
            n=Nurse.objects.create(group=group,name=name,employee_id='DEMO'+str(i+1).zfill(3),subgroup=min(i//3+1,4),is_leader=i<12 and i%3==0,
                level='N3' if i%3==0 else 'N2' if i%3==1 else 'N1',years=10 if i%3==0 else 5 if i%3==1 else 3,
                title='主管护师' if i%3==0 else '护师',base_type='rotation' if i<12 else 'day',qualifications=['A','DAY','N','TRAIN'] if i<12 else ['DAY','TRAIN'],
                skills=['CRRT','机械通气','气道护理'] if i%3==0 else ['气道护理'],
                certifications=[{'name':s,'expires_on':'2030-12-31'} for s in (['CRRT','机械通气','气道护理'] if i%3==0 else ['气道护理'])],
                workdays=[0,1,2,3,4],notes='合成演示人员，非真实员工。')
            nurses.append(n)
            sync_nurse_skills(n)
        User=get_user_model(); password=os.getenv('DEMO_PASSWORD','DemoICU!2026')
        accounts=[]
        for username,role,nurse in [('group3','group_manager',None),('head_nurse','head_nurse',None)]:
            user,new=User.objects.get_or_create(username=username)
            if new: user.set_password(password); user.first_name={'group_manager':'第3组账号','head_nurse':'护士长（演示）'}[role]; user.save()
            AccountProfile.objects.get_or_create(user=user,defaults=dict(role=role,group=group,nurse=nurse))
            accounts.append(user)
        manager=accounts[0]
        for number in range(1,9):
            bed,_=Bed.objects.get_or_create(group=group,number=number)
            if number==8: continue
            Patient.objects.create(group=group,bed=bed,code='SYN-DEMO-'+str(number),name='合成患者'+str(number),gender='男' if number%2 else '女',age=30+number*5,
                diagnoses=['术后观察（合成）'] if number!=1 else ['重症肺炎（合成）'],
                treatments=['CRRT','机械通气'] if number==1 else [],load='high' if number==1 else 'medium' if number==2 else 'light',
                reviewed_by=manager,reviewed_at=timezone.now(),evidence=['seed_demo 合成资料已按演示流程核对。'])
        # Safely create only new demo publications; subsequent invocations never overwrite them.
        draft=schedule.generate(group,manager,str(monday)); pub=schedule.publish(draft,manager,{'revision':draft.revision})
        bd=BedDraft.objects.create(group=group,date=today,shift='A')
        beds.sync(bd,manager,{'revision':bd.revision,'fingerprint':beds.fingerprint(bd)})
        rec=beds.recommend(bd)
        beds.modify(bd,manager,{'revision':bd.revision,'fingerprint':rec['fingerprint'],'assignments':rec['assignments'],'reason':'初始化合成演示分床'})
        beds.publish(bd,manager,{'revision':bd.revision,'fingerprint':beds.fingerprint(bd),'confirm_three':True})
        ShiftRequest.objects.create(group=group,nurse=nurses[0],date=monday+timedelta(days=7),kind='day',target_shift='DAY',reason='合成示例：下周临时白班申请',submitted_by=manager)
        self.stdout.write(self.style.SUCCESS('已建立16名合成护士、8床7名合成患者、正式排班及当日分床。示例账号：group3 / head_nurse。'))
