"""Delete the live roster row while preserving separately versioned business facts."""
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from scheduling.models import AccountProfile, BedDraft, NurseDeletionSnapshot, ScheduleDraft, ShiftRequest
from scheduling.api.serializers import NurseSerializer
from scheduling.services.common import audit, lock_group, require_revision


@transaction.atomic
def delete_nurse(group, actor, nurse, revision):
    lock_group(group)
    nurse.refresh_from_db()
    require_revision(nurse, revision)
    snapshot=dict(NurseSerializer(nurse).data)
    NurseDeletionSnapshot.objects.create(group=group,nurse_id_at_deletion=nurse.id,snapshot=snapshot,actor=actor)
    # Preserve the original request, its approval, participants and provenance.
    # Its effects cannot continue to be applied after either party is deleted.
    affected=ShiftRequest.objects.filter(Q(nurse=nurse)|Q(partner=nurse))
    affected_ids=list(affected.values_list('id',flat=True))
    affected.update(invalidated_by_deletion=True)
    corrected_entries=[]
    for draft in ScheduleDraft.objects.filter(group=group):
        entries=[]
        for entry in draft.entries:
            if entry.get('nurse')==nurse.id: continue
            if entry.get('request_id') in affected_ids:
                before=dict(entry)
                entry={**entry,'shift':entry.get('original',entry['shift']),'source':'automatic','reason':''}
                entry.pop('request_id',None)
                corrected_entries.append({'draft':draft.id,'before':before,'after':entry})
            entries.append(entry)
        draft.entries=entries
        draft.revision+=1;draft.save(update_fields=['entries','revision','updated_at'])
    for draft in BedDraft.objects.filter(group=group):
        draft.roster=[n for n in draft.roster if n!=nurse.id]
        draft.assignments={key:value for key,value in draft.assignments.items() if value!=nurse.id}
        draft.revision+=1;draft.save(update_fields=['roster','assignments','revision','updated_at'])
    # Existing personal user objects and their historical audit FKs are retained.
    # They are never used as business logins and remain disabled after unlinking.
    profiles=AccountProfile.objects.filter(nurse=nurse,role__in=['nurse','disabled_nurse']).select_related('user')
    for profile in profiles:
        if not profile.user.is_superuser:
            profile.user.is_active=False;profile.user.save(update_fields=['is_active'])
            if profile.role!='disabled_nurse':profile.role='disabled_nurse';profile.save(update_fields=['role'])
    nurse_id=nurse.id
    avatar_key=nurse.avatar_key
    nurse.delete()
    if avatar_key:
        from scheduling.services.avatars import discard
        transaction.on_commit(lambda key=avatar_key: discard(key))
    audit(actor,group,'删除护士档案',{'nurse_id':nurse_id,'name':snapshot['name'],
        'employee_id':snapshot['employee_id'],'invalidated_requests':affected_ids,
        'corrected_request_entries':corrected_entries,
        'historical_facts_retained':True})
    return {'deleted':True,'id':nurse_id,'invalidated_requests':len(affected_ids)}
