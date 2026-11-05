from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from scheduling.api.core import ok
from scheduling.models import NurseImportBatch
from scheduling.services import excel_import
from scheduling.services.common import BusinessError, scope


@api_view(['GET','POST','PATCH'])
def excel_import_view(request,pk=None,action=None):
    group=scope(request.user,(request.query_params if request.method=='GET' else request.data).get('group'),
        write=request.method!='GET')
    batch=get_object_or_404(NurseImportBatch,group=group,pk=pk) if pk else None
    if request.method=='POST' and batch is None and action is None:
        upload=request.FILES.get('file')
        if upload is None or upload.size>excel_import.MAX_BYTES: raise BusinessError('请选择不超过5MB的Excel文件。')
        result=excel_import.create_batch(group,request.user,upload.name,upload.read())
    elif request.method=='GET' and batch is not None and action is None:
        result=batch
    elif request.method=='PATCH' and batch is not None and action is None:
        result=excel_import.edit_batch(batch,request.user,request.data)
    elif request.method=='POST' and batch is not None and action=='confirm':
        result,errors=excel_import.confirm_batch(batch,request.user,request.data)
        if errors: raise BusinessError('存在待核对记录，未保存任何护士档案。',422,errors)
    elif request.method=='POST' and batch is not None and action=='ai-suggest':
        result=excel_import.suggest_mapping(batch,request.user,request.data)
    else: raise BusinessError('不支持的导入操作。',405)
    return ok(excel_import.batch_data(result))
