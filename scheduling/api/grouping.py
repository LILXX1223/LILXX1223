from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from scheduling.api.core import ok
from scheduling.models import AIGroupingTask
from scheduling.services import grouping
from scheduling.services.common import BusinessError, scope


@api_view(['GET','POST'])
def formal_view(request,action=None):
    group=scope(request.user,(request.query_params if request.method=='GET' else request.data).get('group'),write=request.method!='GET')
    if request.method=='GET' and not action:return ok(grouping.formal_state(group))
    if request.method=='POST' and action=='undo':return ok(grouping.undo_formal(group,request.user,request.data))
    raise BusinessError('不支持的正式分组操作。',405)


@api_view(['GET','POST'])
def grouping_view(request,pk=None,action=None):
    group=scope(request.user,(request.query_params if request.method=='GET' else request.data).get('group'),
        write=request.method!='GET')
    task=get_object_or_404(AIGroupingTask,group=group,pk=pk) if pk else None
    if request.method=='GET' and not task and not action:
        return ok([grouping.task_data(item) for item in AIGroupingTask.objects.filter(group=group).select_related('created_by').order_by('-id')[:30]])
    if request.method=='GET' and task and not action:return ok(grouping.task_data(task))
    if request.method=='POST' and not task and not action:result=grouping.create_task(group,request.user,request.data)
    elif request.method=='POST' and task and action=='review':result=grouping.review(task,request.user,request.data)
    elif request.method=='POST' and task and action=='confirm':result=grouping.confirm(task,request.user,request.data)
    else:raise BusinessError('不支持的分组操作。',405)
    return ok(grouping.task_data(result))
