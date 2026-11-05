from rest_framework import serializers
from scheduling.models import Nurse, Patient, ShiftType, Skill, NurseSkill

SKILLS = ['CRRT', 'ECMO', '机械通气', 'PICC', '气道护理', '俯卧位护理', '血液净化']


class NurseSerializer(serializers.ModelSerializer):
    skill_details = serializers.SerializerMethodField()
    skill_levels = serializers.DictField(child=serializers.CharField(max_length=100, allow_blank=True), required=False, write_only=True)
    avatar_url = serializers.SerializerMethodField()
    class Meta:
        model = Nurse
        exclude = ['avatar_key']
        read_only_fields = ['group', 'revision']

    def validate(self, data):
        if 'subgroup' in data and data['subgroup'] is not None and data['subgroup'] not in range(1, 5):
            raise serializers.ValidationError('小组须为1至4组或待分组。')
        if 'assessment_level' in data and data['assessment_level'] is not None and data['assessment_level'] not in range(1, 6):
            raise serializers.ValidationError('病情评估能力能级须为1至5档或未填写。')
        if len(data.get('teaching_collaboration', '')) > 2000:
            raise serializers.ValidationError('带教与协作能力不能超过2000字。')
        if len(data.get('special_restrictions', '')) > 2000:
            raise serializers.ValidationError('特殊排班限制不能超过2000字。')
        for key in ['qualifications', 'skills', 'certifications', 'workdays']:
            if key in data and not isinstance(data[key], list): raise serializers.ValidationError(key + '须为列表。')
        if any(x not in ['A','DAY','N','TRAIN'] for x in data.get('qualifications', [])):
            raise serializers.ValidationError('排班资格无效。')
        group=self.context.get('group') or (self.instance.group if self.instance else None)
        catalog={s.name:s for s in Skill.objects.filter(group=group)} if group else {}
        allowed_disabled=set(self.instance.skills) if self.instance else set()
        names=data.get('skills',[])
        if any(not isinstance(x,str) or x not in catalog or (not catalog[x].enabled and x not in allowed_disabled) for x in names):
            raise serializers.ValidationError('专科技能不存在或已停用。')
        if len(names)!=len(set(names)): raise serializers.ValidationError('专科技能不能重复。')
        if any(type(x) is not int or x not in range(7) for x in data.get('workdays', [])):
            raise serializers.ValidationError('工作日须为0至6。')
        previous_certificates={c.get('name'):c for c in (self.instance.certifications if self.instance else []) if isinstance(c,dict)}
        certificate_names=[]
        for cert in data.get('certifications', []):
            if not isinstance(cert, dict) or not isinstance(cert.get('name'),str) or cert.get('name') not in catalog:
                raise serializers.ValidationError('独立资质须包含合法技能名称。')
            if not catalog[cert['name']].enabled and cert!=previous_certificates.get(cert['name']):
                raise serializers.ValidationError('已停用技能不能新增或修改独立资质。')
            certificate_names.append(cert['name'])
            try:
                from datetime import date
                date.fromisoformat(cert.get('expires_on', ''))
            except (TypeError, ValueError): raise serializers.ValidationError('独立资质须有有效期。')
        if len(certificate_names)!=len(set(certificate_names)): raise serializers.ValidationError('独立资质不能重复。')
        if data.get('years', 0) > 60: raise serializers.ValidationError('ICU年资范围无效。')
        if any(not str(k).isdigit() or not Skill.objects.filter(group=group,pk=int(k)).exists() for k in data.get('skill_levels',{})):
            raise serializers.ValidationError('技能掌握程度对应的技能不存在。')
        selected=set(data.get('skills',self.instance.skills if self.instance else []))
        selected.update(c['name'] for c in data.get('certifications',self.instance.certifications if self.instance else []))
        if any(Skill.objects.get(pk=int(k)).name not in selected for k in data.get('skill_levels',{})):
            raise serializers.ValidationError('请先选择该技能，再填写掌握程度。')
        return data

    def get_skill_details(self,obj):
        return [{'id':x.skill_id,'name':x.skill.name,'code':x.skill.code,'enabled':x.skill.enabled,
                 'level':x.level,'independent':x.independent,'expires_on':str(x.expires_on) if x.expires_on else None}
                for x in obj.skill_links.select_related('skill')]

    def get_avatar_url(self,obj):
        # Images are never served as public static files. Version query busts browser caches.
        return f'/api/v1/nurses/{obj.pk}/avatar/?group={obj.group_id}&v={obj.revision}' if obj.avatar_key else None


class PatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = '__all__'
        read_only_fields = ['group','bed','code','reviewed_by','reviewed_at','revision','admitted_at','discharged_at']
    def validate(self, data):
        for key in ['diagnoses','suspected','treatments','historical','suggestions','evidence']:
            if key in data and (not isinstance(data[key], list) or len(data[key]) > 100):
                raise serializers.ValidationError(key + '须为最多100项的列表。')
            if key in data and any(not isinstance(x, str) or len(x)>1000 for x in data[key]):
                raise serializers.ValidationError(key + '每项须为1000字以内文本。')
        if data.get('age') is not None and data['age'] > 130: raise serializers.ValidationError('年龄超出范围。')
        return data


class ShiftSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShiftType
        fields = '__all__'
        read_only_fields = ['group']
        validators = []
        # A new effective version must state its complete timing policy; model
        # defaults must not silently become an unintended clinical schedule.
        extra_kwargs = {key: {'required': True} for key in
                        ['start', 'end', 'next_day', 'hours', 'working', 'effective_from']}
    def validate(self, data):
        if data.get('hours', 0) < 0 or data.get('hours',0) > 24: raise serializers.ValidationError('有效时长须为0至24小时。')
        if data.get('code') in ['OFF','POST'] and (data.get('hours',0) != 0 or data.get('working')):
            raise serializers.ValidationError('休与夜下休必须为非工作班且0小时。')
        if not data.get('working') and data.get('hours', 0) != 0:
            raise serializers.ValidationError('非工作班次的有效工时必须为0小时。')
        if data.get('working'):
            minutes = lambda t: t.hour * 60 + t.minute
            duration = minutes(data['end']) - minutes(data['start']) + (1440 if data.get('next_day') else 0)
            if duration <= 0 or float(data.get('hours',0)) * 60 > duration:
                raise serializers.ValidationError('有效工时不能超过班次时间段，跨日请勾选次日结束。')
        return data
