from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from scheduling.api.core import ok
from scheduling.models import Skill
from scheduling.services.common import scope, role_of, BusinessError
from scheduling.services import skills as service


@api_view(['GET','POST','PATCH','DELETE'])
def skill_catalog(request,pk=None,action=None):
    group=scope(request.user,(request.query_params if request.method=='GET' else request.data).get('group'),write=request.method!='GET')
    if role_of(request.user)=='nurse': raise BusinessError('当前账号无人员技能管理权限。',403)
    if action and (action!='nurses' or request.method!='GET'): raise BusinessError('不支持的技能操作。',405)
    skill=get_object_or_404(Skill,group=group,pk=pk) if pk else None
    if request.method=='GET':
        result=service.catalog(group)
        if skill:
            item=next(x for x in result['items'] if x['id']==skill.id)
            return ok(item['nurses'] if action=='nurses' else item)
        return ok(result)
    if request.method=='POST' and not skill:return ok(service.save_skill(group,request.user,request.data))
    if request.method=='PATCH' and skill:return ok(service.save_skill(group,request.user,request.data,skill))
    if request.method=='DELETE' and skill:return ok(service.delete_skill(group,request.user,skill,request.data.get('revision')))
    raise BusinessError('不支持的技能管理操作。',405)
