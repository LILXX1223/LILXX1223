from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view

from scheduling.api.core import ok
from scheduling.services import allocation_execution
from scheduling.services.common import BusinessError, scope
from scheduling.task_models import AllocationExecution


@api_view(['GET','POST'])
def tasks(request, pk=None):
    values=request.query_params if request.method=='GET' else request.data
    group=scope(request.user,values.get('group'),write=request.method=='POST')
    if request.method=='POST':
        if pk: raise BusinessError('不能重复启动已有任务。',405)
        return ok(allocation_execution.public_task(allocation_execution.start(group,request.user,values.get('kind'),values)))
    if pk:
        after=values.get('after','0')
        if not str(after).isdigit(): raise BusinessError('事件游标无效。')
        task=get_object_or_404(AllocationExecution,pk=pk,group=group)
        allocation_execution.expire_stale(group,task.kind)
        task.refresh_from_db()
        return ok(allocation_execution.public_task(task,int(after)))
    kind=values.get('kind')
    if kind not in ('GROUP_ASSIGNMENT','BED_ASSIGNMENT'): raise BusinessError('任务类型无效。')
    allocation_execution.expire_stale(group,kind)
    query=AllocationExecution.objects.filter(group=group,kind=kind).order_by('-id')[:30]
    return ok([allocation_execution.public_task(task) for task in query])


@api_view(['GET'])
def compare(request):
    group=scope(request.user,request.query_params.get('group'))
    try: left_id=int(request.query_params.get('left'));right_id=int(request.query_params.get('right'))
    except (TypeError,ValueError):raise BusinessError('请选择两轮已完成的优化任务。')
    left=get_object_or_404(AllocationExecution,pk=left_id,group=group,status='completed')
    right=get_object_or_404(AllocationExecution,pk=right_id,group=group,status='completed')
    if left.kind!=right.kind:raise BusinessError('分组与床位任务不能混合比较。')
    return ok(allocation_execution.compare_tasks(left,right,request.query_params.get('left_plan','A'),request.query_params.get('right_plan','A')))
