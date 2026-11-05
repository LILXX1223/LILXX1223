"""Per-group capability catalog and safe compatibility with legacy nurse fields."""
from datetime import date
from django.db import transaction
from django.utils import timezone
from scheduling.models import Nurse, Skill, NurseSkill, SkillDeletionSnapshot
from scheduling.services.common import BusinessError, audit, invalidate_drafts, lock_group, require_revision


# The names below identify treatment requirements, not automatic nurse qualifications.
BUILTINS = [('CRRT','CRRT'),('ECMO','ECMO'),('VENT','机械通气'),('PICC','PICC'),
            ('AIRWAY','气道护理'),('PRONE','俯卧位护理'),('DIALYSIS','血液净化')]


def ensure_catalog(group):
    """Create the seven original project skills in a newly initialized group."""
    for code,name in BUILTINS:
        Skill.objects.get_or_create(group=group, code=code,
            defaults={'name':name,'category':'专科护理','match_terms':[name]})


def sync_nurse_skills(nurse, levels=None):
    """Only call in the same transaction as a validated nurse edit."""
    levels=levels or {}
    certificates={c['name']:c for c in nurse.certifications}
    names=set(nurse.skills)|set(certificates)
    skills=list(Skill.objects.filter(group=nurse.group,name__in=names))
    if len(skills)!=len(names): raise BusinessError('技能项目已变更，请刷新后重新维护。',409)
    NurseSkill.objects.filter(nurse=nurse).exclude(skill__in=skills).delete()
    for skill in skills:
        cert=certificates.get(skill.name); link,_=NurseSkill.objects.get_or_create(nurse=nurse,skill=skill)
        link.independent=bool(cert);link.expires_on=date.fromisoformat(cert['expires_on']) if cert else None
        if str(skill.id) in levels: link.level=levels[str(skill.id)]
        link.save()


def catalog(group):
    active=Nurse.objects.filter(group=group,status='active',effective_from__lte=timezone.localdate()).count()
    results=[]
    for skill in Skill.objects.filter(group=group).order_by('id'):
        links=list(skill.nurse_links.select_related('nurse').order_by('nurse__id'))
        participants=[link.nurse for link in links]
        count=sum(n.status=='active' and n.effective_from<=timezone.localdate() for n in participants)
        results.append({'id':skill.id,'group':group.id,'code':skill.code,'name':skill.name,
            'category':skill.category,'description':skill.description,'enabled':skill.enabled,
            'participates_bed':skill.participates_bed,'participates_group':skill.participates_group,
            'revision':skill.revision,'nurse_count':len(participants),'active_nurse_count':count,
            'coverage':round(count/active*100,1) if active and skill.enabled else 0,
            'nurses':[{'id':n.id,'name':n.name,'employee_id':n.employee_id,'status':n.status} for n in participants]})
    return {'items':results,'active_nurses':active}


def validate_values(data,instance=None):
    import re
    defaults={'code':'','name':'','category':'','description':'','enabled':True,'participates_bed':True,'participates_group':True}
    values={key:data.get(key,getattr(instance,key) if instance else default) for key,default in defaults.items()}
    for key in ['code','name','category','description']:
        if not isinstance(values[key],str): raise BusinessError('技能字段必须为文本。')
        values[key]=values[key].strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,48}',values['code']): raise BusinessError('技能代码须为1至48位字母、数字、下划线或连字符。')
    if not values['name'] or len(values['name'])>80 or len(values['category'])>80 or len(values['description'])>500:
        raise BusinessError('请填写技能名称，分类及说明不得超过长度限制。')
    if instance and values['code']!=instance.code: raise BusinessError('技能代码为稳定标识，创建后不能修改。')
    for key in ['enabled','participates_bed','participates_group']:
        if type(values[key]) is not bool: raise BusinessError('技能开关必须为布尔值。')
    return values


@transaction.atomic
def save_skill(group,user,data,skill=None):
    lock_group(group)
    if skill:
        skill.refresh_from_db();require_revision(skill,data.get('revision'))
    values=validate_values(data,skill)
    if Skill.objects.filter(group=group,code=values['code']).exclude(pk=skill.pk if skill else None).exists():
        raise BusinessError('该大组已有相同技能代码。',409)
    if Skill.objects.filter(group=group,name=values['name']).exclude(pk=skill.pk if skill else None).exists():
        raise BusinessError('该大组已有相同技能名称。',409)
    before=None
    if skill:
        before={'name':skill.name,'code':skill.code,'category':skill.category,'description':skill.description,
                'enabled':skill.enabled,'participates_bed':skill.participates_bed,'participates_group':skill.participates_group}
        old=skill.name
        for key,value in values.items():setattr(skill,key,value)
        skill.match_terms=list(dict.fromkeys([*(skill.match_terms or []),old,skill.name]))
        skill.revision+=1;skill.save()
        if old!=skill.name:
            for nurse in Nurse.objects.filter(skill_links__skill=skill).distinct():
                nurse.skills=[skill.name if name==old else name for name in nurse.skills]
                nurse.certifications=[{**cert,'name':skill.name if cert.get('name')==old else cert.get('name')} for cert in nurse.certifications]
                nurse.revision+=1;nurse.save(update_fields=['skills','certifications','revision'])
    else:
        skill=Skill.objects.create(group=group,match_terms=[values['name']],**values)
    invalidate_drafts(group,schedule=False,bed=True)
    audit(user,group,'维护专科技能',{'skill':skill.id,'before':before,'after':values,'revision':skill.revision})
    return next(x for x in catalog(group)['items'] if x['id']==skill.id)


@transaction.atomic
def delete_skill(group,user,skill,revision):
    lock_group(group);skill.refresh_from_db();require_revision(skill,revision)
    links=list(NurseSkill.objects.filter(skill=skill).select_related('nurse'))
    names=[x.nurse_id for x in links]
    associations=[{'nurse_id':x.nurse_id,'name':x.nurse.name,'employee_id':x.nurse.employee_id,
        'level':x.level,'independent':x.independent,'expires_on':str(x.expires_on) if x.expires_on else None} for x in links]
    SkillDeletionSnapshot.objects.create(group=group,skill_id_at_deletion=skill.id,code=skill.code,name=skill.name,
        match_terms=skill.match_terms,participates_bed=skill.participates_bed,actor=user,associations=associations)
    for nurse in Nurse.objects.filter(pk__in=names):
        nurse.skills=[name for name in nurse.skills if name!=skill.name]
        nurse.certifications=[cert for cert in nurse.certifications if cert.get('name')!=skill.name]
        nurse.revision+=1;nurse.save(update_fields=['skills','certifications','revision'])
    key=skill.id;code=skill.code;name=skill.name
    skill.delete()
    invalidate_drafts(group,schedule=False,bed=True)
    audit(user,group,'删除专科技能',{'skill':key,'code':code,'name':name,'associated_nurses':names})
    return {'deleted':True,'id':key}
