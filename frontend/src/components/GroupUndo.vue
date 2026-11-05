<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../services/api'
import { useWorkspace } from '../stores/workspace'

const props=defineProps({ modelValue: Boolean })
const emit=defineEmits(['update:modelValue','changed'])
const s=useWorkspace()
const loading=ref(false), saving=ref(false), state=ref(null), failure=ref('')
const mode=ref('clear'), reason=ref(''), otherReason=ref(''), loadedGroup=ref('')
let requestId=0
const reasons=['当前分组不合理','希望重新进行智能分组','人员配置发生变化','技能或带教搭配需要调整','临时管理需要','其他原因']
const textReason=computed(()=>reason.value==='其他原因'?otherReason.value.trim():reason.value)
const affected=computed(()=>{
  if(!state.value) return []
  if(mode.value==='clear') return state.value.members.filter(n=>n.subgroup!=null).map(n=>({...n,next:null}))
  if(!Array.isArray(state.value.previous_members)) return []
  const previous=new Map(state.value.previous_members.map(n=>[n.id,n.subgroup]))
  return state.value.members.filter(n=>n.subgroup!==(previous.get(n.id)??null)).map(n=>({...n,next:previous.get(n.id)??null}))
})
const groupLabel=n=>n==null?'未分组':`第${n}小组`
const stamp=value=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'尚无正式确认记录'
const canSubmit=computed(()=>state.value && !loading.value && !saving.value &&
  (mode.value==='clear'?state.value.assigned_count>0:state.value.can_restore))

async function load(){
  const id=++requestId, group=String(s.group)
  loading.value=true;failure.value='';state.value=null;loadedGroup.value=group
  try {
    const result=await api('grouping/formal/?'+new URLSearchParams({group}))
    if(id===requestId && props.modelValue && group===String(s.group)) state.value=result
  } catch(error) { if(id===requestId) failure.value=error.message }
  finally { if(id===requestId) loading.value=false }
}
async function confirmUndo(){
  if(!canSubmit.value || loadedGroup.value!==String(s.group)) return
  if(reason.value==='其他原因'&&!otherReason.value.trim()) return ElMessage.warning('请填写其他原因，或清空原因。')
  const action=mode.value==='clear'?'清空当前小组':'恢复上一正式分组'
  try {
    await ElMessageBox.confirm(`${action}将生成新的正式版本。已发布历史排班保持不变，未发布排班需重新校验。确认执行？`,
      '再次确认',{type:'warning',confirmButtonText:'确认执行',cancelButtonText:'取消'})
  } catch { return }
  if(!props.modelValue || loadedGroup.value!==String(s.group) || !state.value) return
  saving.value=true
  try {
    await api('grouping/formal/undo/','POST',{group:Number(s.group),mode:mode.value,confirmed:true,
      fingerprint:state.value.fingerprint,reason:textReason.value})
    ElMessage.success('正式分组已更新，历史版本已保留。')
    emit('update:modelValue',false);emit('changed')
  } catch(error) {
    const details=(error.issues||[]).map(x=>x.message).filter(Boolean).join('；')
    ElMessage.error(error.message+(details?' '+details:''))
    if(error.status===409) await load()
  } finally { saving.value=false }
}
watch(()=>props.modelValue,open=>{
  if(open){mode.value='clear';reason.value='';otherReason.value='';load()}
  else {requestId++;state.value=null;failure.value='';loading.value=false}
})
watch(()=>s.group,()=>{if(props.modelValue) emit('update:modelValue',false)})
</script>

<template>
  <el-dialog :model-value="modelValue" title="撤销当前小组分配" width="min(760px, 94vw)"
    :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving"
    @update:model-value="emit('update:modelValue',$event)">
    <div v-loading="loading">
      <el-alert v-if="failure" :title="failure" type="error" :closable="false"/>
      <el-button v-if="failure" class="section-gap" @click="load">重新加载</el-button>
      <template v-if="state">
        <el-descriptions :column="2" border>
          <el-descriptions-item label="当前大组">{{state.group_name}}</el-descriptions-item>
          <el-descriptions-item label="当前正式分组版本">{{state.current_version?'#'+state.current_version:'尚无正式版本记录'}}</el-descriptions-item>
          <el-descriptions-item label="已分组护士">{{state.assigned_count}} 人</el-descriptions-item>
          <el-descriptions-item label="当前有效小组">{{state.subgroup_count}} 个</el-descriptions-item>
          <el-descriptions-item label="未分组护士">{{state.unassigned_count}} 人</el-descriptions-item>
          <el-descriptions-item label="最近确认时间">{{stamp(state.confirmed_at)}}</el-descriptions-item>
        </el-descriptions>
        <el-alert class="section-gap" :title="state.schedule_impact" type="warning" :closable="false"/>
        <div class="section-gap"><strong>撤销方式</strong></div>
        <el-radio-group v-model="mode" class="section-gap">
          <el-radio value="clear">清空当前小组</el-radio>
          <el-radio value="restore" :disabled="!state.can_restore">恢复上一正式分组{{state.previous_version?' #'+state.previous_version:''}}</el-radio>
        </el-radio-group>
        <p v-if="!state.can_restore && state.restore_issues?.length" class="muted small">无法恢复：{{state.restore_issues.join('；')}}</p>
        <p class="small">清空仅取消小组归属和实际小组角色，保留所属大组、护士档案和带组资格。</p>
        <div class="section-gap"><strong>受影响人员预览（{{affected.length}} 人）</strong></div>
        <el-table :data="affected" max-height="210" empty-text="当前选择没有人员归属变化">
          <el-table-column prop="name" label="护士" min-width="105"/>
          <el-table-column prop="employee_id" label="工号" min-width="120"/>
          <el-table-column label="当前小组" min-width="110"><template #default="scope">{{groupLabel(scope.row.subgroup)}}</template></el-table-column>
          <el-table-column label="调整后" min-width="110"><template #default="scope">{{groupLabel(scope.row.next)}}</template></el-table-column>
        </el-table>
        <p v-if="mode==='restore' && !Array.isArray(state.previous_members)" class="muted small">历史成员明细暂不可用，请以服务端校验结果为准。</p>
        <el-form label-position="top" class="section-gap">
          <el-form-item label="撤销原因（可选）">
            <el-select v-model="reason" clearable placeholder="请选择原因" style="width:100%">
              <el-option v-for="item in reasons" :key="item" :value="item" :label="item"/>
            </el-select>
          </el-form-item>
          <el-input v-if="reason==='其他原因'" v-model="otherReason" type="textarea" maxlength="500" show-word-limit placeholder="请填写原因"/>
        </el-form>
      </template>
    </div>
    <template #footer>
      <el-button :disabled="saving" @click="emit('update:modelValue',false)">取消</el-button>
      <el-button type="danger" :loading="saving" :disabled="!canSubmit" @click="confirmUndo">{{mode==='clear'?'清空当前小组':'恢复上一正式分组'}}</el-button>
    </template>
  </el-dialog>
</template>
