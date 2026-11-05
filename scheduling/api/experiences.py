from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view

from scheduling.api.core import ok
from scheduling.experience_models import AllocationExperience, AllocationFeedback
from scheduling.services import experiences
from scheduling.services.common import BusinessError, scope


@api_view(['GET','POST'])
def collection(request):
    group=scope(request.user,(request.query_params if request.method=='GET' else request.data).get('group'),write=request.method!='GET')
    if request.method=='GET':
        kind=experiences.validate_type(request.query_params.get('business_type'))
        return ok([experiences.serialize(e) for e in AllocationExperience.objects.filter(group=group,business_type=kind).select_related('created_by','reviewed_by').order_by('-id')[:200]])
    return ok(experiences.serialize(experiences.create(group,request.user,request.data)))


@api_view(['POST'])
def action(request,pk):
    group=scope(request.user,request.data.get('group'),write=True)
    item=get_object_or_404(AllocationExperience,group=group,pk=pk)
    return ok(experiences.serialize(experiences.change_status(group,request.user,item,request.data)))


@api_view(['GET'])
def versions(request,pk):
    group=scope(request.user,request.query_params.get('group'))
    item=get_object_or_404(AllocationExperience,group=group,pk=pk)
    return ok(experiences.versions(group,item))


@api_view(['POST'])
def suggestion(request):
    group=scope(request.user,request.data.get('group'),write=True)
    return ok(experiences.serialize(experiences.suggest(group,request.user,request.data)))


@api_view(['POST'])
def analyze_feedback(request):
    group=scope(request.user,request.data.get('group'),write=True)
    return ok(experiences.serialize(experiences.analyze_feedback(group,request.user,request.data)))


@api_view(['GET','POST'])
def feedback(request):
    group=scope(request.user,(request.query_params if request.method=='GET' else request.data).get('group'),write=request.method!='GET')
    if request.method=='GET':
        kind=experiences.validate_type(request.query_params.get('business_type'))
        return ok([experiences.feedback_data(e) for e in AllocationFeedback.objects.filter(group=group,business_type=kind).select_related('created_by').order_by('-id')[:200]])
    return ok(experiences.feedback_data(experiences.record_feedback(group,request.user,request.data)))
