from scheduling.api.serializers import PatientSerializer
from scheduling.models import PatientRevision
from scheduling.services.bed_configuration import public_number


def record_version(patient, actor, action):
    # Invoked inside the caller's write transaction. Before the first edited
    # version, capture a labelled baseline; never invent past clinical edits.
    if PatientRevision.objects.filter(patient=patient, revision=patient.revision).exists():
        return
    snapshot = dict(PatientSerializer(patient).data)
    snapshot['bed_number'] = public_number(patient.bed.number) if patient.bed_id else None
    PatientRevision.objects.create(patient=patient, revision=patient.revision,
        snapshot=snapshot, actor=actor, action=action)
