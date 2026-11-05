from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from scheduling.api.core import ok
from scheduling.models import MedicalRecognitionBatch
from scheduling.services import medical_batches
from scheduling.services.common import scope


@api_view(['GET', 'POST'])
def batches(request, pk=None, action=None):
    data = request.query_params if request.method == 'GET' else request.data
    group = scope(request.user, data.get('group'), write=request.method == 'POST')
    medical_batches.clean_expired(group)
    queryset = MedicalRecognitionBatch.objects.filter(group=group, created_by=request.user)
    if request.method == 'GET':
        if pk is not None:
            return ok(medical_batches.serialize(get_object_or_404(queryset, pk=pk)))
        # The workspace only restores the latest task; do not return unrelated clinical results.
        return ok([medical_batches.serialize(batch) for batch in queryset.order_by('-id')[:1]])
    if pk is None:
        batch, skipped = medical_batches.create(group, request.user, request.data)
        return ok({'batch':medical_batches.serialize(batch) if batch else None, 'skipped':skipped})
    batch = get_object_or_404(queryset, pk=pk)
    if action != 'run':
        from scheduling.services.common import BusinessError
        raise BusinessError('未知批量识别操作。')
    medical_batches.run(group, request.user, batch, request.data.get('item'), request.data.get('retry', False))
    return ok(medical_batches.serialize(batch))
