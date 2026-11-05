from django.contrib import admin
from .models import (CareGroup, Nurse, NurseDeletionSnapshot, AccountProfile, ShiftType, ShiftRequest, ScheduleDraft, AIServiceConfiguration,
    SchedulePublication, Bed, Patient, PatientRevision, Extraction, BedDraft, BedPublication, AuditLog, Notification,
    Skill, NurseSkill, SkillDeletionSnapshot, NurseImportBatch, AIGroupingTask, GroupingPublication,
    AllocationExperience, AllocationFeedback, AISceneConfiguration, AIRequestLog, ScheduleDraftVersion, BedGroupConfiguration,
    MedicalRecognitionBatch, MedicalRecognitionItem)


class BusinessReadOnlyAdmin(admin.ModelAdmin):
    """Use business APIs for audited mutation; SimpleUI is a read-only operational inspection surface."""
    def has_module_permission(self,request): return request.user.is_superuser
    def has_view_permission(self,request,obj=None): return request.user.is_superuser
    def has_change_permission(self,request,obj=None): return False
    def has_add_permission(self,request): return False
    def has_delete_permission(self,request,obj=None): return False


@admin.register(AIServiceConfiguration)
class AIConfigurationAdmin(BusinessReadOnlyAdmin):
    exclude=['encrypted_key']
    list_display=['provider','base_url','model','revision','updated_at']


@admin.register(AccountProfile)
class ProfileAdmin(admin.ModelAdmin):
    list_display=['user','role','group','nurse']
    def has_module_permission(self,request): return request.user.is_superuser
    def has_view_permission(self,request,obj=None): return request.user.is_superuser
    def has_change_permission(self,request,obj=None): return request.user.is_superuser
    def has_add_permission(self,request): return request.user.is_superuser
    def has_delete_permission(self,request,obj=None): return False
    def save_model(self,request,obj,form,change):
        super().save_model(request,obj,form,change)
        AuditLog.objects.create(actor=request.user,group=obj.group,action='配置账号权限',details={'user':obj.user_id,'role':obj.role,'nurse':obj.nurse_id})


@admin.register(MedicalRecognitionItem)
class MedicalRecognitionItemAdmin(BusinessReadOnlyAdmin):
    exclude=['encrypted_text','plan_token']
    list_display=['id','batch','bed','status','attempt','error_code','started_at','finished_at']


for model in [CareGroup,Nurse,NurseDeletionSnapshot,ShiftType,ShiftRequest,ScheduleDraft,ScheduleDraftVersion,SchedulePublication,Bed,BedGroupConfiguration,Patient,PatientRevision,Extraction,BedDraft,BedPublication,AuditLog,Notification,Skill,NurseSkill,SkillDeletionSnapshot,NurseImportBatch,AIGroupingTask,GroupingPublication,AllocationExperience,AllocationFeedback,AISceneConfiguration,AIRequestLog,MedicalRecognitionBatch]:
    admin.site.register(model,BusinessReadOnlyAdmin)
admin.site.site_header='ICU 护士排班管理系统'
admin.site.site_title='ICU 系统管理'
