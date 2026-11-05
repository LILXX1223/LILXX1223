import os
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from scheduling.api.core import ok
from scheduling.models import Nurse
from scheduling.services import avatars
from scheduling.services.common import BusinessError, scope


def parse_revision(value):
    # Multipart form values arrive as strings, JSON values as integers.
    if type(value) is int and value >= 0: return value
    if isinstance(value,str) and value.isascii() and value.isdigit(): return int(value)
    raise BusinessError('头像修订版本无效，请刷新后重试。',409)


@api_view(['GET','POST','DELETE'])
def nurse_avatar(request, pk):
    source=request.query_params if request.method=='GET' else request.data
    group=scope(request.user,source.get('group'),write=request.method!='GET')
    nurse=get_object_or_404(Nurse,pk=pk,group=group)
    if request.method=='GET':
        if not nurse.avatar_key: raise BusinessError('该护士尚未设置头像。',404)
        path=avatars.safe_path(nurse.avatar_key)
        try:
            fd=os.open(str(path),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
            stream=os.fdopen(fd,'rb')
        except OSError: raise BusinessError('头像文件暂不可用。',404)
        response=FileResponse(stream,content_type='image/webp')
        response['Cache-Control']='private, no-store'
        response['X-Content-Type-Options']='nosniff'
        return response
    revision=parse_revision(source.get('revision'))
    if request.method=='POST':
        return ok(avatars.replace(group,nurse,request.user,revision,request.FILES.get('file')))
    return ok(avatars.remove(group,nurse,request.user,revision))
