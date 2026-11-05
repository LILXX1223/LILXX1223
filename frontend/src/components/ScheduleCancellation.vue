<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, addDays, today } from '../services/api'

const props=defineProps({modelValue:Boolean,group:[Number,String],week:String,nurses:Array,shifts:Array,phases:Array,formalPublication:{type:Object,default:null}})
const emit=defineEmits(['update:modelValue','changed'])
const form=ref({}), preset=ref('week'), report=ref(null), loading=ref(false), saving=ref(false), error=ref(''),formalRange=ref('whole')
let requestId=0, timer=null
const stats=computed(()=>[['涉及日期',report.value?.affected_dates??'—','天'],['涉及护士',report.value?.affected_nurses??'—','人'],['需撤销班次',report.value?.affected_cells??'—','格'],['已发布记录',report.value?.published_records??'—','条'],['草稿记录',report.value?.draft_records??'—','条'],['关联调班申请',report.value?.related_requests??'—','条']])
const nurseOptions=computed(()=>{const rows=new Map((props.nurses||[]).map(n=>[n.id,n]));for(const n of props.formalPublication?.nurses||[])if(!rows.has(n.id))rows.set(n.id,{...n,employee_id:'历史档案'});return [...rows.values()]})
const fields=()=>({group:Number(props.group),start_date:form.value.start_date,end_date:form.value.end_date,
  nurse_scope:form.value.nurse_scope,subgroup:form.value.subgroup,shift:form.value.shift,nurse_ids:form.value.nurse_ids||[],
  formal_only:!!props.formalPublication,publication_id:props.formalPublication?.id,historical_correction:!!form.value.historical_correction})
function range(value){preset.value=value;const day=today()
  if(value==='week'){form.value.start_date=props.week;form.value.end_date=addDays(props.week,6)}
  else if(value==='last7'){form.value.start_date=addDays(day,-6);form.value.end_date=day}
  else if(value==='last14'){form.value.start_date=addDays(day,-13);form.value.end_date=day}
}
function reset(){requestId++;report.value=null;error.value='';preset.value='week';formalRange.value='whole';const start=props.formalPublication?.start_date||props.week;form.value={start_date:start,end_date:props.formalPublication?.end_date||addDays(start,6),nurse_scope:'all',subgroup:null,shift:'ALL',nurse_ids:[],reason:'',historical_correction:false}}
function changeFormalRange(value){if(value==='whole'){form.value.start_date=props.formalPublication.start_date;form.value.end_date=props.formalPublication.end_date;form.value.nurse_scope='all';form.value.nurse_ids=[];form.value.shift='ALL'}}
watch(()=>props.modelValue,open=>{if(open)reset();else{requestId++;clearTimeout(timer)}})
watch(()=>JSON.stringify(fields()),()=>{
  if(!props.modelValue)return
  requestId++;const id=requestId;report.value=null;error.value='';loading.value=false;clearTimeout(timer)
  if(!form.value.start_date||!form.value.end_date||form.value.start_date>form.value.end_date)return
  if(form.value.nurse_scope==='subgroup'&&!form.value.subgroup)return
  timer=setTimeout(()=>load(id),250)
},{immediate:true})
async function load(id){loading.value=true
  try{const data=await api('schedules/cancel-preview/','POST',fields());if(id===requestId)report.value=data}
  catch(e){if(id===requestId)error.value=e.message}
  finally{if(id===requestId)loading.value=false}
}
async function cancel(){
  if(!report.value?.affected_cells||!form.value.reason?.trim())return ElMessage.warning('请填写撤销原因并核对预览。')
  const parameters={...fields(),reason:form.value.reason.trim(),fingerprint:report.value.fingerprint,confirmed:true},id=requestId
  try{await ElMessageBox.confirm(`范围 ${form.value.start_date} 至 ${form.value.end_date}，涉及 ${report.value.affected_nurses} 人、${report.value.published_records} 条正式记录和 ${report.value.draft_records} 条草稿记录。历史正式快照与分床记录保留。确认继续？`,'二次确认',{type:'warning',confirmButtonText:'确认撤销',cancelButtonText:'取消'})}catch{return}
  if(id!==requestId)return ElMessage.warning('筛选条件已变化，请重新核对。')
  saving.value=true
  try{const result=await api('schedules/cancel/','POST',parameters);ElMessage.success(result.message);emit('changed');emit('update:modelValue',false)}
  catch(e){error.value=e.message;report.value=null;ElMessage.error(e.message)}finally{saving.value=false}
}
onBeforeUnmount(()=>{requestId++;clearTimeout(timer)})
</script>
<template>
  <el-dialog :model-value="modelValue" :title="formalPublication?'撤销已发布正式排班':'撤销时段排班'" width="min(600px,94vw)" top="4vh" class="schedule-cancel-dialog" :close-on-click-modal="false" @close="emit('update:modelValue',false)">
    <el-alert title="将撤销所选范围内的有效排班。历史发布快照及调班申请保留；相关床位责任须重新核对。" type="warning" :closable="false"/>
    <p v-if="formalPublication" class="small">{{formalPublication.group_name}} · 正式版本 #{{formalPublication.id}} · {{formalPublication.start_date}} 至 {{formalPublication.end_date}} · 发布人 {{formalPublication.published_by}} · {{new Date(formalPublication.created_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})}}<br/>有效班次 {{formalPublication.effective_records}} 条 · 已撤销 {{formalPublication.cancelled_records}} 条</p>
    <el-form label-position="top" class="section-gap">
      <el-form-item v-if="formalPublication" label="撤销方式"><el-radio-group v-model="formalRange" @change="changeFormalRange"><el-radio value="whole">撤销整期正式排班</el-radio><el-radio value="partial">撤销指定时段</el-radio></el-radio-group></el-form-item>
      <el-form-item label="日期范围"><div v-if="!formalPublication" class="range-buttons"><el-button v-for="[key,label] in [['week','当前显示周'],['last7','近一周'],['last14','近两周'],['custom','自定义']]" :key="key" size="small" :type="preset===key?'primary':'default'" @click="range(key)">{{label}}</el-button></div>
        <div class="date-fields"><el-date-picker v-model="form.start_date" value-format="YYYY-MM-DD" placeholder="开始日期" :disabled="!!formalPublication&&formalRange==='whole'" @change="preset='custom'"/><span>至</span><el-date-picker v-model="form.end_date" value-format="YYYY-MM-DD" placeholder="结束日期" :disabled="!!formalPublication&&formalRange==='whole'" @change="preset='custom'"/></div></el-form-item>
      <el-form-item label="人员范围"><el-radio-group v-model="form.nurse_scope" :disabled="!!formalPublication&&formalRange==='whole'"><el-radio value="all">全部护士</el-radio><el-radio value="rotation">固定轮转</el-radio><el-radio value="day">长期固定白班</el-radio><el-radio value="subgroup">指定小组</el-radio></el-radio-group>
        <el-select v-if="form.nurse_scope==='subgroup'" v-model="form.subgroup" placeholder="选择小组"><el-option v-for="(_,i) in phases||[]" :key="i" :label="'第'+(i+1)+'小组'" :value="i+1"/></el-select></el-form-item>
      <el-form-item label="指定护士（可选）"><el-select v-model="form.nurse_ids" multiple filterable collapse-tags :disabled="!!formalPublication&&formalRange==='whole'" placeholder="不选表示上述范围内全部护士"><el-option v-for="n in nurseOptions" :key="n.id" :value="n.id" :label="n.name+' · '+(n.employee_id||'历史档案')"/></el-select></el-form-item>
      <el-form-item label="班次范围"><el-select v-model="form.shift" :disabled="!!formalPublication&&formalRange==='whole'"><el-option label="全部班次" value="ALL"/><el-option v-for="shift in shifts||[]" :key="shift.code" :value="shift.code" :label="shift.name"/></el-select></el-form-item>
      <el-form-item label="撤销原因"><div class="range-buttons"><el-button v-for="reason in ['原正式排班存在错误','需要重新优化排班','人员配置发生变化','班次安排需要调整','临时管理需要','其他原因']" :key="reason" size="small" @click="form.reason=reason">{{reason}}</el-button></div><el-input v-model="form.reason" type="textarea" maxlength="500" placeholder="请填写撤销原因"/></el-form-item>
    </el-form>
    <el-alert v-if="error" :title="error" type="error" :closable="false"/>
    <div class="cancel-stats" v-loading="loading"><div v-for="[label,value,unit] in stats" :key="label"><small>{{label}}</small><strong>{{value}} <small>{{unit}}</small></strong></div></div>
    <p v-if="report" class="muted small">关联正式分床 {{report.related_bed_publications}} 个、分床草稿 {{report.related_bed_drafts}} 个；{{report.protected_records}} 条正式班次已开始或日期已过去。系统未接入考勤，是否实际执行须人工核实。</p>
    <p v-if="report?.published_records" class="muted small">涉及日期：{{report.affected_date_values.join('、')}}；正式班次：{{Object.entries(report.affected_shift_counts).map(([code,count])=>code+' '+count+'条').join('、')}}</p>
    <details v-if="report?.affected_members?.length"><summary>查看受影响护士（{{report.affected_nurses}} 人）</summary><p class="small muted">{{report.affected_members.map(n=>n.name+' · '+n.employee_id).join('、')}}</p></details>
    <el-alert v-if="report?.active_bed_conflicts" title="存在正在执行或时间无法核实、且已发布责任床位的班次；当前系统尚无安全交接确认流程，不能撤销。" type="error" :closable="false"/>
    <el-checkbox v-if="report?.protected_records&&!report?.active_bed_conflicts" v-model="form.historical_correction">我已核实授权范围，按历史更正处理已开始或过去的班次</el-checkbox>
    <p class="muted small">“需撤销班次”按护士和日期去重；草稿和正式记录分别计数。重新生成会创建新的草稿版本，正式历史不变。</p>
    <template #footer><el-button @click="emit('update:modelValue',false)">取消</el-button><el-button type="danger" :disabled="loading||!report?.affected_cells||!report?.can_execute||!form.reason?.trim()" :loading="saving" @click="cancel">确认撤销</el-button></template>
  </el-dialog>
</template>
<style scoped>
.range-buttons,.date-fields{display:flex;gap:7px;align-items:center;flex-wrap:wrap;width:100%}.range-buttons .el-button+.el-button{margin-left:0}.date-fields{margin-top:8px}.date-fields :deep(.el-date-editor){flex:1;min-width:140px}.cancel-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}.cancel-stats div{padding:10px;background:#f3f8fd;border:1px solid #e1edf9;border-radius:6px}.cancel-stats small,.cancel-stats strong{display:block}.cancel-stats strong{font-size:18px;color:#135db9}.cancel-stats strong small{display:inline;font-size:12px;font-weight:400}
:global(.schedule-cancel-dialog){display:flex;flex-direction:column;max-height:92vh}:global(.schedule-cancel-dialog .el-dialog__body){min-height:0;overflow-y:auto}
</style>
