from rest_framework.decorators import api_view
from scheduling.api.core import ok, request_data
from django.shortcuts import get_object_or_404
from scheduling.models import ShiftRequest, AuditLog
from scheduling.services import request_history
from scheduling.services.common import scope, BusinessError


@api_view(['GET','POST'])
def history(request):
    data=request.query_params if request.method=='GET' else request.data
    group=scope(request.user,data.get('group'),write=request.method=='POST')
    if request.method=='GET':
        state=data.get('state','normal')
        if state not in ('normal','deleted'):raise BusinessError('申请记录筛选无效。')
        records=ShiftRequest.objects.filter(group=group,is_deleted=(state=='deleted')).order_by('-id')
        return ok([request_data(row) for row in records])
    if data.get('preview') is True:
        rows=request_history.inspect(group,data.get('items'),data.get('action'))
        return ok({'items':rows,'eligible':sum(r['eligible'] for r in rows),'total':len(rows),
            'schedule_impact':'仅隐藏或恢复列表记录，不改变审批和正式排班。'})
    return ok(request_history.apply(group,request.user,data))


@api_view(['GET'])
def request_versions(request, pk):
    from scheduling.services.shift_requests import details
    group=scope(request.user,request.query_params.get('group'))
    current=get_object_or_404(ShiftRequest,pk=pk,group=group)
    root=current
    while root.revises_id:
        root=root.revises
    nodes=[root]; seen={root.pk}
    for node in nodes:
        for child in node.request_revisions.filter(group=group).order_by('id'):
            if child.pk not in seen:
                seen.add(child.pk);nodes.append(child)
    events=[]
    for event in AuditLog.objects.filter(group=group,action__in=[
            '代录调班申请','处理调班申请','编辑调班申请','撤销调班申请',
            '撤销调班申请修订','批准调班申请修订']).select_related('actor').order_by('id'):
        body=event.details or {}
        related={body.get('request'),body.get('replacement'),body.get('before_id')}
        for key in ('before','after'):
            value=body.get(key)
            if isinstance(value,dict): related.add(value.get('id'))
        if seen.intersection(related):
            events.append({'id':event.id,'action':event.action,'actor':event.actor.get_username() if event.actor_id else None,
                           'created_at':event.created_at.isoformat(),'details':body})
    return ok({'root_id':root.id,'versions':[details(node) for node in nodes],'events':events})
