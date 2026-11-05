<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { BedDouble, Users, CheckCheck, ShieldAlert, Sparkles } from 'lucide-vue-next'
import { ElMessageBox, ElMessage } from 'element-plus'
import { useWorkspace } from '../stores/workspace'
import { api, shiftName } from '../services/api'
import PatientForm from '../components/PatientForm.vue'
import NurseAvatar from '../components/NurseAvatar.vue'
import BedIllustration from '../components/BedIllustration.vue'
import ValidationResult from '../components/ValidationResult.vue'
import AllocationProgress from '../components/AllocationProgress.vue'
import AllocationAssistant from '../components/AllocationAssistant.vue'
import AllocationHistory from '../components/AllocationHistory.vue'
import AllocationComparison from '../components/AllocationComparison.vue'
const s=useWorkspace(), board=computed(()=>s.data.board), dialog=ref(''), context=ref({}), report=ref(null), recommendation=ref(null), extraction=ref(null), confirmed=ref(false), triple=ref(false)
const mode=ref('single'), bedNumber=ref(null), text=ref(''), edit=ref({}), adjust=ref({}), patient=ref(null)
const extractionPlan=ref(null), extractionFailures=ref([])
const includeEmptyBeds=ref(false), extractionSkipped=ref([]), extractionSharedError=ref('')
const extractionReauth=ref(false)
const batch=ref(null),batchBusy=ref(false),batchUseSingle=ref(true),batchReview=ref(false)
let batchGeneration=0
const batchCounts=computed(()=>{const items=batch.value?.items||[];return {
  total:items.length,success:items.filter(i=>i.status==='succeeded').length,
  running:items.filter(i=>i.status==='running').length,waiting:items.filter(i=>i.status==='waiting').length,
  failed:items.filter(i=>i.status==='failed').length}})
const reauth=ref(false),reauthPassword=ref(''),reauthBusy=ref(false),reauthError=ref('')
let extractionGeneration=0
const patientVersions=ref([]), historyLoading=ref(false), viewedRevision=ref(null)
const bedFilter=ref('all'), nurseFilter=ref('all')
const aiTab=ref('records'), execution=ref(null), executionBusy=ref(false), showExecution=ref(false), previewVisible=ref(false)
const rosterNeedsResync=computed(()=>{const b=s.data?.board;if(!b?.id)return false
  const old=new Set(b.roster||[]),current=new Set((b.nurses||[]).map(n=>n.id))
  return !b.source_publication||b.source_status==='cancelled'||b.source_cancelled_since_sync||b.synced_publication!==b.source_publication||old.size!==current.size||[...old].some(id=>!current.has(id))})
const aiSynthetic=ref(null), aiConsent=ref(false)
const oneTimeExperiences=ref([])
const oneTimeRevisions=ref({})
const historySelection=ref(null)
function toggleExperience(id,revision){if(oneTimeExperiences.value.includes(id)){oneTimeExperiences.value=oneTimeExperiences.value.filter(x=>x!==id);const next={...oneTimeRevisions.value};delete next[String(id)];oneTimeRevisions.value=next}else{oneTimeExperiences.value=[...oneTimeExperiences.value,id];oneTimeRevisions.value={...oneTimeRevisions.value,[String(id)]:revision}}}
function applyExperienceNow(item){if(!oneTimeExperiences.value.includes(item.id))oneTimeExperiences.value=[...oneTimeExperiences.value,item.id];oneTimeRevisions.value={...oneTimeRevisions.value,[String(item.id)]:item.revision};recommend()}
let taskTimer=null, taskGeneration=0
const displayedAssignments=computed(()=>previewVisible.value&&recommendation.value?.assignments||board.value?.assignments||{})
const occupied=computed(()=>board.value?.beds.filter(b=>b.patient)||[])
const assigned=computed(()=>occupied.value.filter(b=>displayedAssignments.value[String(b.id)]).length)
const nurseName=id=>s.data.nurses.find(n=>n.id===id)?.name||'未分配'
const loadName=v=>({light:'较轻',medium:'稍重',high:'高负荷'}[v]||'待核对')
const managed=id=>occupied.value.filter(b=>displayedAssignments.value[String(b.id)]===id)
const nursingLoad=id=>managed(id).reduce((a,b)=>a+({light:1,medium:2,high:4}[b.patient.load]||0),0)
const maxBeds=computed(()=>s.data.group.rules.max_beds||3)
const bedFilters=computed(()=>[
  ['all','全部床位',board.value?.beds.length||0],
  ['occupied','在床患者',occupied.value.length],
  ['unassigned','未分配',occupied.value.length-assigned.value],
  ['empty','空床',(board.value?.beds||[]).filter(b=>b.enabled&&!b.patient).length],
])
const visibleBeds=computed(()=> (board.value?.beds||[]).filter(b=>
  bedFilter.value==='all'||(bedFilter.value==='occupied'&&b.patient)||
  (bedFilter.value==='empty'&&b.enabled&&!b.patient)||(bedFilter.value==='unassigned'&&b.patient&&!displayedAssignments.value[String(b.id)])))
const nurseFilters=[['all','全部'],['unassigned','未分配'],['assigned','已分配'],['load','负荷排序']]
const visibleNurses=computed(()=>{
  const nurses=(board.value?.nurses||[]).filter(n=>nurseFilter.value==='all'||nurseFilter.value==='load'||
    (nurseFilter.value==='unassigned'&&!managed(n.id).length)||(nurseFilter.value==='assigned'&&managed(n.id).length))
  return nurseFilter.value==='load'?[...nurses].sort((a,b)=>nursingLoad(b.id)-nursingLoad(a.id)||a.name.localeCompare(b.name,'zh-CN')):nurses
})
const activeSkills=n=>(n.skill_details||[]).filter(skill=>skill.enabled).map(skill=>skill.name)
const activeCertifications=n=>(n.skill_details||[]).filter(skill=>skill.enabled&&skill.independent&&skill.expires_on&&skill.expires_on>=s.date&&
  (n.certifications||[]).some(cert=>cert.name===skill.name&&cert.expires_on>=s.date)).map(skill=>skill.name)
function capture(){context.value={...s.bedPayload()};confirmed.value=false;triple.value=false}
async function reload(){await s.run(()=>Promise.resolve(true),'')}
function openSync(){capture();dialog.value='sync'}
async function sync(){const r=await s.run(()=>api('beds/sync/','POST',context.value),'');if(r){dialog.value='';if(r.conflicts?.length)ElMessage.warning(r.message);else ElMessage.success(r.message||'已同步正式当班护士')}}
function fillSample(){bedNumber.value=bedNumber.value||board.value.beds.find(b=>b.enabled&&b.configured&&b.visible)?.number;if(!bedNumber.value)return ElMessage.warning('当前没有已显示且开放的床位。');text.value=`${bedNumber.value}床：\n姓名：合成患者${bedNumber.value}；性别：男；年龄：55岁；\n确定诊断：肺炎；疑似诊断：ARDS；\n此前使用CRRT，现已停止；当前持续机械通气；\n护理负荷：稍重；以上为合成演示病例。`}
async function extract(){
  if(mode.value==='batch')return startBatch()
  batchReview.value=false
  if(s.busy)return
  s.busy=true;extractionFailures.value=[]
  try{
    const input=text.value,group=Number(s.group),selectedMode=mode.value,generation=++extractionGeneration
    extractionSharedError.value='';extractionReauth.value=false;extractionSkipped.value=[]
    const plan=await api('beds/extract-prepare/','POST',{group,mode:selectedMode,bed_number:bedNumber.value,text:input,synthetic:true,include_empty_beds:includeEmptyBeds.value})
    if(generation!==extractionGeneration)return
    extractionPlan.value={...plan,input,group,selectedMode,generation};extractionSkipped.value=plan.skipped||[]
    extraction.value={records:[],sources:[],mode:'external'};confirmed.value=false
    if(!plan.records.length){ElMessage.warning('没有符合条件的在床病例；如需向空床导入合成病例，请明确勾选后重新识别。');return}
    await processExtractions(plan.records)
  }catch(error){if(error.code==='not_authenticated'||error.code==='csrf_failed'){extractionSharedError.value=error.message;extractionReauth.value=true}ElMessage.error(error.message)}finally{s.busy=false}
}
async function restoreBatch(){
  const group=Number(s.group), generation=batchGeneration
  if(!group)return
  try{const list=await api(`medical-batches/?group=${group}`)
    if(generation!==batchGeneration||Number(s.group)!==group||!list.length)return
    batch.value=list[0];extractionSkipped.value=batch.value.skipped||[]
    if(batch.value.items.some(i=>['waiting','running'].includes(i.status)))processBatch()
  }catch(error){if(generation===batchGeneration)ElMessage.error('批量任务读取失败：'+error.message)}
}
async function startBatch(){
  if(batchBusy.value||s.busy)return
  const group=Number(s.group),generation=++batchGeneration
  batchBusy.value=true;extractionSharedError.value='';extractionReauth.value=false
  try{
    const result=await api('medical-batches/','POST',{group,mode:'batch',text:text.value,synthetic:true,
      include_empty_beds:includeEmptyBeds.value,parameter_source:batchUseSingle.value?'medical_single':'medical_batch'})
    if(generation!==batchGeneration||Number(s.group)!==group)return
    extractionSkipped.value=result.skipped||[];batch.value=result.batch
    if(!result.batch){ElMessage.warning('没有符合条件的病例；空床需明确选择合成病例导入。');return}
  }catch(error){if(error.status===401||['not_authenticated','csrf_failed'].includes(error.code)){extractionSharedError.value=error.message;extractionReauth.value=true}ElMessage.error(error.message)}
  finally{batchBusy.value=false}
  if(generation===batchGeneration&&batch.value)await processBatch()
}
async function processBatch(retry=false,onlyItem=null){
  if(batchBusy.value||!batch.value)return
  if(retry){extractionSharedError.value='';extractionReauth.value=false}
  batchBusy.value=true
  const generation=batchGeneration,group=Number(s.group),id=batch.value.id
  const retryIds=new Set(retry?batch.value.items.filter(i=>i.status==='failed'&&i.retryable&&(!onlyItem||i.id===onlyItem)).map(i=>i.id):[])
  const active=()=>generation===batchGeneration&&Number(s.group)===group&&batch.value?.id===id
  try{
    while(active()){
      const current=await api(`medical-batches/${id}/?group=${group}`)
      if(!active())break
      batch.value=current
      if(batch.value.items.some(i=>i.status==='running')){await new Promise(resolve=>setTimeout(resolve,1200));continue}
      const shared=batch.value.items.find(i=>i.status==='failed'&&['auth','not_authenticated','csrf_failed','permission_denied'].includes(i.error_code)&&!retryIds.has(i.id))
      if(shared){extractionSharedError.value=shared.error;extractionReauth.value=['not_authenticated','csrf_failed'].includes(shared.error_code);break}
      const item=batch.value.items.find(i=>retryIds.has(i.id)&&i.status==='failed')||(!onlyItem&&batch.value.items.find(i=>i.status==='waiting'))
      if(!item)break
      const isRetry=retryIds.has(item.id)
      try{
        // Only this one case is in flight. The server commits each case separately.
        let awaitingResult=true
        const observe=async()=>{
          while(awaitingResult&&active()){
            await new Promise(resolve=>setTimeout(resolve,900))
            if(!awaitingResult||!active())break
            try{const state=await api(`medical-batches/${id}/?group=${group}`);if(awaitingResult&&active())batch.value=state}catch{}
          }
        }
        const observer=observe()
        let updated
        try{updated=await api(`medical-batches/${id}/run/`,'POST',{group,item:item.id,retry:isRetry},
          {timeout:(batch.value.ai_parameters.timeout_seconds+90)*1000})}
        finally{awaitingResult=false;await observer}
        if(!active())break
        batch.value=updated
        retryIds.delete(item.id)
        const done=batch.value.items.find(i=>i.id===item.id)
        if(done?.status==='failed'&&['auth','not_authenticated','csrf_failed','permission_denied'].includes(done.error_code)){
          extractionSharedError.value=done.error;extractionReauth.value=['not_authenticated','csrf_failed'].includes(done.error_code);break
        }
      }catch(error){
        if(error.code==='batch_busy'){await new Promise(resolve=>setTimeout(resolve,1200));continue}
        if(['frontend_timeout','gateway_timeout'].includes(error.code)){await new Promise(resolve=>setTimeout(resolve,1200));continue}
        if(error.status===401||error.status===403||['not_authenticated','csrf_failed','permission_denied'].includes(error.code)){
          extractionSharedError.value=error.message;extractionReauth.value=['not_authenticated','csrf_failed'].includes(error.code)||error.status===401;break
        }
        ElMessage.error('批量任务连接失败：'+error.message);break
      }
    }
  }catch(error){
    if(active()&&(error.status===401||error.status===403||['not_authenticated','csrf_failed','permission_denied'].includes(error.code))){
      extractionSharedError.value=error.message;extractionReauth.value=['not_authenticated','csrf_failed'].includes(error.code)||error.status===401
    }else if(active())ElMessage.error('批量任务状态读取失败：'+error.message)
  }finally{if(generation===batchGeneration)batchBusy.value=false}
}
function openBatchReview(item){
  if(!item?.extraction||item.extraction.reviewed)return
  extraction.value={mode:item.extraction.mode,sources:[{id:item.extraction.id,bed_number:item.bed_number}],
    records:JSON.parse(JSON.stringify(item.extraction.records)).map(record=>({...record,extraction_id:item.extraction.id}))}
  confirmed.value=false;batchReview.value=true;dialog.value='review'
}
async function processExtractions(items){
  const plan=extractionPlan.value,failed=[];extractionSharedError.value='';extractionReauth.value=false
  for(let index=0;index<items.length;index++){
    const item=items[index]
    if(Number(s.group)!==plan.group||plan.generation!==extractionGeneration)break
    try{
      const result=await api('beds/extract-prepared/','POST',{group:plan.group,token:item.token,text:item.text},{timeout:(plan.ai_parameters.timeout_seconds+30)*1000})
      if(Number(s.group)!==plan.group||plan.generation!==extractionGeneration)break
      extraction.value.mode=result.mode
      if(!extraction.value.sources.some(x=>x.id===result.id)){
        extraction.value.sources.push({id:result.id,bed_number:item.bed_number})
        extraction.value.records.push(...result.records.map(record=>({...record,extraction_id:result.id})))
      }
    }catch(error){
      const shared=error.status===401||error.status===403||['not_authenticated','csrf_failed','permission_denied','auth'].includes(error.code)
      if(shared){extractionSharedError.value=error.message;extractionReauth.value=['not_authenticated','csrf_failed'].includes(error.code)||error.status===401;failed.push(...items.slice(index).map(x=>({...x,error:'本次任务已暂停，待解决共享认证或权限问题后重试。'})));break}
      failed.push({...item,error:error.message})
    }
  }
  if(plan.generation!==extractionGeneration)return
  extractionFailures.value=failed
  if(extraction.value.records.length){dialog.value='review';ElMessage.success(`已识别 ${extraction.value.records.length} 床，请人工核对。`)}
  if(failed.length&&!extractionSharedError.value)ElMessage.warning(`${failed.length} 床识别失败，已保留成功结果，可单独重试。`)
}
async function signInAgain(){
  if(!reauthPassword.value)return
  reauthBusy.value=true;reauthError.value=''
  try{const user=await api('auth/login/','POST',{username:s.user.username,password:reauthPassword.value})
    if(user.id!==s.user.id)throw new Error('账号已变化，请重新准备识别。')
    reauthPassword.value='';reauth.value=false;extractionSharedError.value='';ElMessage.success('已重新登录，可继续重试未完成病例。')
  }catch(error){reauthError.value=error.message}finally{reauthBusy.value=false}
}
async function retryExtractions(){
  if(s.busy||!extractionPlan.value)return
  if(text.value!==extractionPlan.value.input||mode.value!==extractionPlan.value.selectedMode){ElMessage.warning('输入或模式已改变，请重新识别。');return}
  s.busy=true;try{await processExtractions([...extractionFailures.value])}finally{s.busy=false}
}
async function review(){
  const items=extraction.value.sources.map(source=>({extraction:source.id,records:extraction.value.records.filter(record=>record.extraction_id===source.id)}))
  const r=await s.run(()=>api('beds/review-batch/','POST',{...s.groupPayload(),items,confirmed:confirmed.value}),'已核对并写入对应床位')
  if(r){dialog.value='';extraction.value={records:[],sources:[],mode:'external'};confirmed.value=false
    if(batchReview.value&&batch.value){batchReview.value=false;try{batch.value=await api(`medical-batches/${batch.value.id}/?group=${s.group}`)}catch(error){ElMessage.error(error.message)};return}
    if(!extractionFailures.value.length){extraction.value=null;extractionPlan.value=null}}
}
async function recommend(){capture();if(s.data.ai.configured){aiSynthetic.value=null;aiConsent.value=false;dialog.value='ai-consent';return}const r=await s.run(()=>api('beds/recommend/','POST',context.value),'');if(r){recommendation.value=r;previewVisible.value=true;dialog.value='recommend'}}
function stopTask(){taskGeneration++;if(taskTimer)clearTimeout(taskTimer);taskTimer=null}
async function startAI(){
  if(aiSynthetic.value===null||!aiConsent.value)return ElMessage.warning('请选择资料来源并确认外发授权。')
  const previous=execution.value,active=['queued','running'].includes(previous?.status)
  if(active){try{await ElMessageBox.confirm('当前任务正在执行，将由新一轮替代；旧结果不会覆盖新方案。','重新优化')}catch{return}}
  const group=s.group,contextSnapshot={...context.value};executionBusy.value=true;stopTask()
  try{const result=await api('allocation-tasks/','POST',{...contextSnapshot,kind:'BED_ASSIGNMENT',synthetic:aiSynthetic.value,consent:true,experience_ids:[...oneTimeExperiences.value],one_time_experience_revisions:{...oneTimeRevisions.value},...(previous?.id?{previous_task_id:previous.id}:{}),...(active?{supersede_confirmed:true}:{})});if(s.group!==group)return
    execution.value=result;showExecution.value=true;aiTab.value='assistant';dialog.value='';await pollTask(result.id,group,contextSnapshot.id)
  }catch(e){executionBusy.value=false;ElMessage.error(e.message);if(active)await pollTask(previous.id,group,contextSnapshot.id)}
}
async function pollTask(id,group,draftId){
  stopTask();const token=taskGeneration
  async function poll(){try{const result=await api(`allocation-tasks/${id}/?group=${group}`);if(token!==taskGeneration||s.group!==group)return;execution.value=result
    if(result.status==='completed'){executionBusy.value=false;if(board.value?.id!==draftId){ElMessage.warning('床位分配上下文已变化，请重新生成方案。');return}recommendation.value=result.result;context.value={...s.bedPayload()};previewVisible.value=true;showExecution.value=false;dialog.value='recommend';return}
    if(['failed','superseded'].includes(result.status)){executionBusy.value=false;ElMessage.error(result.error||'分配任务失败。');return}
    taskTimer=setTimeout(poll,1000)
  }catch(e){if(token===taskGeneration){executionBusy.value=false;ElMessage.error('任务状态读取失败：'+e.message)}}}
  await poll()
}
async function restoreTask(){const group=s.group;try{const list=await api(`allocation-tasks/?group=${group}&kind=BED_ASSIGNMENT`);if(s.group!==group||!list.length)return;const item=list[0];execution.value=item;if(['queued','running'].includes(item.status)&&item.parameters.id===board.value?.id){executionBusy.value=true;showExecution.value=true;await pollTask(item.id,group,item.parameters.id)}}catch{}}
onMounted(()=>{restoreTask();restoreBatch()});onBeforeUnmount(()=>{stopTask();batchGeneration++})
async function applyRecommendation(){const r=await s.run(()=>api('beds/apply/','POST',{...context.value,revision:recommendation.value.revision,fingerprint:recommendation.value.fingerprint,assignments:recommendation.value.assignments,reason:'负责人应用智能推荐方案'}),'推荐方案已应用至草稿');if(r){previewVisible.value=false;dialog.value=''}}
function openAdjust(bed){capture();adjust.value={bed:bed?.id||occupied.value[0]?.id,nurse:null,swap:null,reason:'其他原因',assignments:{...board.value.assignments}};dialog.value='adjust'}
const adjusted=computed(()=>{const a={...(adjust.value.assignments||{})};const key=String(adjust.value.bed);if(adjust.value.swap){const other=String(adjust.value.swap),v=a[key];a[key]=a[other];a[other]=v}else if(adjust.value.nurse)a[key]=adjust.value.nurse;return Object.fromEntries(Object.entries(a).filter(([,v])=>v!=null))})
async function saveAdjust(){const r=await s.run(()=>api('beds/adjust/','POST',{...context.value,assignments:adjusted.value,reason:adjust.value.reason}),'责任护士已调整');if(r){report.value=r.validation;dialog.value='validation'}}
async function validate(publish=false){capture();const r=await s.run(()=>api('beds/validate/','POST',context.value),'');if(r){report.value=r;dialog.value=publish?'publish':'validation'}}
async function publish(){const r=await s.run(()=>api('beds/publish/','POST',{...context.value,confirm_three:triple.value}),'床位分配已发布，工作量和个人责任同步更新');if(r)dialog.value=''}
async function openPatient(bed,readonly=false){
  patient.value=bed;edit.value=bed.patient?JSON.parse(JSON.stringify(bed.patient)):{name:'',gender:'',age:null,diagnoses:[],suspected:[],treatments:[],historical:[],suggestions:[],evidence:[],load:null,synthetic:true};confirmed.value=false;dialog.value=readonly?'details':'patient'
  patientVersions.value=[];viewedRevision.value=null
  if(readonly&&bed.patient){
    const group=s.group,id=bed.patient.id;historyLoading.value=true
    try{const rows=await api(`patients/${id}/history/?group=${group}`);if(s.group===group&&patient.value?.patient?.id===id)patientVersions.value=rows}catch(e){ElMessage.error(e.message)}finally{historyLoading.value=false}
  }
}
function selectPatientRevision(revision){const version=patientVersions.value.find(v=>v.revision===revision);edit.value=JSON.parse(JSON.stringify(version?version.snapshot:patient.value.patient))}
watch(()=>[s.group,s.date,s.shift],(current,previous)=>{stopTask();extractionGeneration++;oneTimeExperiences.value=[];oneTimeRevisions.value={};executionBusy.value=false;previewVisible.value=false;execution.value=null;dialog.value='';text.value='';patient.value=null;patientVersions.value=[];bedNumber.value=null;extraction.value=null;extractionPlan.value=null;extractionFailures.value=[]
  if(current[0]!==previous?.[0]){batchGeneration++;batchBusy.value=false;batch.value=null;restoreBatch()}})
async function savePatient(){const r=await s.run(()=>api('beds/patient/','POST',{...s.groupPayload(),bed_id:patient.value.id,patient_id:patient.value.patient?.id,patient_revision:patient.value.patient?.revision,fields:edit.value,confirmed:confirmed.value}),'患者资料已保存');if(r)dialog.value=''}
async function leaveBed(bed){try{const answer=await ElMessageBox.prompt('请输入患者离床原因。正式历史责任记录会保留。','确认患者离床',{inputValidator:v=>!!v?.trim()||'请填写原因'});await s.run(()=>api('beds/discharge/','POST',{...s.groupPayload(),patient_id:bed.patient.id,patient_revision:bed.patient.revision,reason:answer.value}),'患者已离床')}catch{}}
function locate(issue){const b=board.value.beds.find(b=>b.id===issue.bed);if(b)openAdjust(b)}
function closeDialog(...names){ if(names.includes(dialog.value)) dialog.value='' }
</script>
<template><div class="page-heading"><div><h1>床位分配</h1><p>{{s.data.group.name}} · {{shiftName(s.shift)}} · {{s.date}}</p></div><div v-if="s.canEdit" class="actions"><el-button @click="openSync">同步当班护士</el-button><el-button @click="extract" :disabled="!text.trim()||!s.data.ai.configured||batchBusy" :loading="s.busy||batchBusy">AI识别病历</el-button><el-button type="primary" @click="recommend" :disabled="!board?.id||executionBusy||rosterNeedsResync" :loading="s.busy||executionBusy">智能分配</el-button><el-button @click="openAdjust()" :disabled="!board?.id||!occupied.length">人工调整</el-button><el-button @click="validate()" :disabled="!board?.id">分配校验</el-button><el-button type="primary" @click="validate(true)" :disabled="!board?.id||rosterNeedsResync">确认并发布</el-button></div></div>
<div class="filters"><el-date-picker v-model="s.date" value-format="YYYY-MM-DD" :clearable="false" @change="reload"/><el-select v-model="s.shift" @change="reload"><el-option v-for="c in ['A','DAY','N']" :key="c" :value="c" :label="shiftName(c)"/></el-select><el-tag v-if="board?.published" type="success">正式分床版本 #{{board.published}}</el-tag><span class="muted small" v-if="board">当班来源：{{board.source_publication?'正式排班 #'+board.source_publication:'暂无正式排班'}}</span><el-tag v-if="board?.source_status==='partially_cancelled'" type="warning">正式排班部分撤销</el-tag><el-tag v-else-if="board?.source_status==='cancelled'" type="danger">正式排班已撤销</el-tag></div>
<el-alert v-if="rosterNeedsResync" title="排班来源已变化，请重新同步当班护士并校验床位责任；已发布的历史分床记录保留。" type="warning" :closable="false"/>
<el-alert v-if="board?.beds.some(b=>b.patient&&(!b.configured||!b.visible||!b.enabled))" title="部分在床患者所在床位未正常开放或显示，已强制展示，请先核对床位配置。" type="error" :closable="false"/>
<el-alert v-if="board?.statistics?.open_not_shown" :title="`${board.statistics.open_not_shown} 张开放床位未设置显示，仍计入开放及空床统计；请核对实际管理范围。`" type="warning" :closable="false"/>
<template v-if="board"><div class="kpis five"><div v-for="[label,value,icon] in [['开放床位',board.statistics.open,BedDouble],['在床患者',board.statistics.occupied,Users],['已分配患者',assigned+'/'+board.statistics.occupied,CheckCheck],['待核对',occupied.filter(b=>!b.patient.reviewed_by).length,ShieldAlert],['空床',board.statistics.empty,BedDouble]]" :key="label" class="kpi"><span class="symbol"><component :is="icon" :size="22"/></span><div><label>{{label}}</label><strong>{{value}}</strong></div></div></div>
<div class="bed-layout"><section class="card bed-column">
  <div class="card-title"><h2>床位分配区</h2><span class="muted small">配置 {{board.statistics.configured}} 床 · 显示 {{board.statistics.visible}} 床 · {{previewVisible?'当前显示候选责任（未应用）':s.canEdit?'当前显示草稿责任':'当前显示正式责任'}}</span></div>
  <div class="bed-filter" aria-label="床位筛选">
    <button v-for="[value,label,count] in bedFilters" :key="value" type="button" :class="{active:bedFilter===value}" :aria-pressed="bedFilter===value" @click="bedFilter=value">{{label}} <span>{{count}}</span></button>
  </div>
  <div class="bed-grid">
    <article v-for="b in visibleBeds" :key="b.id" :class="['bed-card',{empty:!b.patient}]">
      <header><b>{{b.number}}床</b><el-tag v-if="!b.enabled" type="info" size="small">未开放</el-tag><el-tag v-else-if="b.patient" :type="b.patient.load==='high'?'danger':b.patient.load==='medium'?'warning':'success'" size="small">{{loadName(b.patient.load)}}</el-tag><el-tag v-else type="info" size="small">空床</el-tag>
        <el-dropdown trigger="click" @command="command=>command==='view'?openPatient(b,true):command==='edit'?openPatient(b):command==='adjust'?openAdjust(b):leaveBed(b)"><button class="icon-button" :aria-label="b.number+'床操作'">···</button><template #dropdown><el-dropdown-menu><el-dropdown-item command="view" :disabled="!b.patient">查看患者详情</el-dropdown-item><el-dropdown-item command="edit" :disabled="!s.canEdit||!b.enabled">{{b.patient?'编辑患者信息':'转入合成患者'}}</el-dropdown-item><el-dropdown-item command="adjust" :disabled="!s.canEdit||!b.patient||!board.id">更换责任护士</el-dropdown-item><el-dropdown-item command="leave" :disabled="!s.canEdit||!b.patient">患者离床</el-dropdown-item></el-dropdown-menu></template></el-dropdown>
      </header>
      <BedIllustration/>
      <template v-if="b.patient">
        <strong>{{b.patient.name}} <span class="muted small">{{b.patient.gender}} {{b.patient.age??'—'}}岁</span></strong>
        <el-tag v-if="b.patient.synthetic" type="info" size="small">合成示例</el-tag>
        <p>{{b.patient.diagnoses.join('、')||'诊断待补充'}}</p>
        <p v-if="b.patient.reviewed_by" class="muted">当前治疗：{{b.patient.treatments.join('、')||'未记录特殊治疗'}}</p>
        <p v-else class="unreviewed">患者资料待人工核对</p>
        <div class="bed-nurse">{{previewVisible?'候选责任护士':s.canEdit?'草稿责任护士':'责任护士'}}：{{nurseName(displayedAssignments[String(b.id)])}}</div>
      </template>
      <template v-else><p>暂无患者</p><el-button v-if="s.canEdit&&b.enabled" size="small" plain @click="openPatient(b)">转入合成患者</el-button></template>
    </article>
  </div>
  <el-empty v-if="!visibleBeds.length" description="当前筛选下暂无床位" :image-size="60"/>
</section>
<section class="card nurse-column">
  <h2>当班护士 <small class="muted">{{board.nurses.length}}人</small></h2>
  <div class="nurse-filter" aria-label="护士筛选"><button v-for="[value,label] in nurseFilters" :key="value" type="button" :class="{active:nurseFilter===value}" :aria-pressed="nurseFilter===value" @click="nurseFilter=value">{{label}}</button></div>
  <el-empty v-if="!board.nurses.length" description="当前日期与班次未找到有效的正式当班护士记录，请核查排班发布情况。" :image-size="70"/>
  <el-empty v-else-if="!visibleNurses.length" description="当前筛选下暂无护士" :image-size="60"/>
  <article v-for="n in visibleNurses" :key="n.id" class="nurse-card">
    <header><NurseAvatar :nurse="n"/><b>{{n.name}}</b><el-tag size="small">{{n.level}}</el-tag><span class="nurse-years">{{n.years}}年</span></header>
    <p>分管床位：<b>{{managed(n.id).map(b=>b.number+'床').join('、')||'未分配 / 支援'}}</b></p>
    <div class="nurse-load"><el-progress :percentage="Math.min(100,managed(n.id).length/maxBeds*100)" :show-text="false" :color="managed(n.id).length>=maxBeds?'#f39c32':'#2384f4'"/><span>{{managed(n.id).length}} / {{maxBeds}}床</span></div>
    <p>护理负荷 {{nursingLoad(n.id)}} / {{s.data.group.rules.max_load}} <el-tag size="small" :type="nursingLoad(n.id)>s.data.group.rules.max_load?'danger':'success'">{{nursingLoad(n.id)>s.data.group.rules.max_load?'超限':'范围内'}}</el-tag></p>
    <small class="muted skill-line">有效技能：{{activeSkills(n).join('、')||'暂无'}}</small>
    <small class="muted skill-line">有效独立资质：{{activeCertifications(n).join('、')||'暂无'}}</small>
  </article>
  <p class="muted small">护理负荷为配置参数辅助评估，须结合实际病情核对。</p>
</section>
<section class="card ai-box"><h2><Sparkles :size="18"/> 病历识别与分配辅助</h2><el-tabs v-model="aiTab"><el-tab-pane label="病历识别" name="records"/><el-tab-pane label="AI分配助手" name="assistant"/><el-tab-pane label="匹配评价" name="evaluation"/></el-tabs>
<div v-if="aiTab==='records'&&mode==='batch'" class="section-gap"><el-checkbox v-model="includeEmptyBeds" :disabled="!s.canEdit||s.busy||batchBusy">同时为病历中明确指定的空床导入合成病例</el-checkbox><p class="small muted">默认只识别明确床号且当前有患者的病例；空床不会自动调用AI。</p><el-checkbox v-model="batchUseSingle" :disabled="batchBusy">本批采用系统设置中的单床识别参数</el-checkbox><p class="small muted">已勾选时复用当前单床场景；取消勾选使用批量场景。不会修改已保存的AI配置。</p></div>
<div v-if="aiTab==='records'&&batch" class="batch-progress section-gap">
  <b>批量识别进度 · 任务 #{{batch.id}}</b>
  <p class="small muted">总病例 {{batchCounts.total}} · 成功 {{batchCounts.success}} · 识别中 {{batchCounts.running}} · 等待 {{batchCounts.waiting}} · 失败 {{batchCounts.failed}}</p>
  <p class="small muted">{{batch.ai_parameters.configuration_source==='medical_single'?'单床':'批量'}}场景参数：{{batch.ai_parameters.model}} · {{batch.ai_parameters.thinking}} · 每例 {{batch.ai_parameters.timeout_seconds}} 秒；每次处理 1 例。</p>
  <div v-for="item in batch.items" :key="item.id" class="batch-item small">
    <span>{{item.bed_number}}床 · {{({waiting:'等待中',running:'识别中',succeeded:item.extraction?.reviewed?'已核对':'成功 · 待核对',failed:'失败'})[item.status]}}<template v-if="item.attempt"> · 尝试 {{item.attempt}} 次</template></span>
    <el-button v-if="item.extraction&&!item.extraction.reviewed" link type="primary" size="small" @click="openBatchReview(item)">查看核对</el-button>
    <el-button v-if="item.status==='failed'&&item.retryable" link type="primary" size="small" :disabled="batchBusy" @click="processBatch(true,item.id)">重试此床</el-button>
    <p v-if="item.error" class="batch-error">{{item.error}}</p>
  </div>
  <el-button v-if="batchCounts.failed" size="small" :disabled="batchBusy||!batch.items.some(i=>i.status==='failed'&&i.retryable)" @click="processBatch(true)">重试失败病例</el-button>
  <el-button v-if="batchCounts.waiting&&!batchBusy" size="small" @click="processBatch()">继续识别</el-button>
  <p class="small muted">刷新可恢复已保存结果；加密暂存2小时后失效，打开任务时清理。关闭页面后队列暂停，重新打开可继续。</p>
</div>
<div v-if="aiTab==='records'&&extractionSkipped.length" class="section-gap"><el-alert :title="`已跳过 ${extractionSkipped.length} 张未选择导入合成病例的空床。`" type="info" :closable="false"/></div>
<div v-if="aiTab==='records'&&extractionSharedError" class="section-gap"><el-alert :title="extractionSharedError+' 已暂停后续病例，输入与成功结果保留。'" type="error" :closable="false"/><el-button v-if="extractionReauth" size="small" @click="reauth=true">重新登录当前账号</el-button></div>
<div v-if="aiTab==='records'&&extractionFailures.length" class="section-gap"><el-alert type="warning" :closable="false" :title="extractionSharedError?`${extractionFailures.length} 个病例等待恢复后重试。`:'部分病例识别失败，已保留输入和成功结果。'"/><p v-for="item in extractionSharedError?[]:extractionFailures" :key="item.bed_number" class="small">{{item.bed_number}}床：{{item.error}}</p><el-button size="small" :loading="s.busy" :disabled="!!extractionSharedError" @click="retryExtractions">重试失败病例</el-button><el-button v-if="extraction?.records.length" size="small" @click="dialog='review'">查看已识别病例</el-button></div>
<template v-if="aiTab==='records'"><el-tag :type="s.data.ai.configured?'primary':'warning'">{{s.data.ai.configured?'外部AI配置已保存 · 请确认授权':'AI服务未配置'}}</el-tag><el-radio-group v-model="mode" class="section-gap" :disabled="!s.canEdit"><el-radio-button value="single">单床识别</el-radio-button><el-radio-button value="batch">批量识别</el-radio-button></el-radio-group><label v-if="mode==='single'">选择床位</label><el-select v-if="mode==='single'" v-model="bedNumber" :disabled="!s.canEdit" placeholder="请选择床号"><el-option v-for="b in board.beds.filter(b=>b.enabled&&b.configured&&b.visible)" :key="b.id" :value="b.number" :label="b.number+'床'"/></el-select><label>病历信息（仅粘贴合成病例）</label><el-input v-model="text" type="textarea" :rows="10" maxlength="30000" show-word-limit :disabled="!s.canEdit" :placeholder="mode==='batch'?'每位患者以独立床号开头，例如：\n1床：…\n2床：…':'粘贴患者诊断、当前治疗与既往治疗记录…'"/><div class="actions section-gap"><el-button size="small" :disabled="!s.canEdit" @click="fillSample">填入合成示例</el-button><el-button size="small" @click="text=''">清空</el-button></div><el-button type="primary" class="full" :disabled="!s.canEdit||!text.trim()||!s.data.ai.configured||batchBusy" :loading="s.busy||batchBusy" @click="extract">AI识别病历</el-button><p v-if="!s.data.ai.configured" class="small muted">请由系统管理员在系统设置中配置 AI 服务。也可手工维护患者资料。</p><p class="muted small section-gap">识别结果须核对床号与身份。确定与疑似诊断、当前与既往治疗分别保存。AI建议不会自动成为正式资料。</p><el-divider/><p v-if="!s.data.ai.configured" class="muted small">AI未配置时可使用规则求解，结果仍需人工确认。</p><el-button :disabled="!s.canEdit||!board.id" :loading="s.busy" @click="recommend">生成分配建议</el-button></template>
<template v-else-if="aiTab==='assistant'"><AllocationHistory :key="execution?.id||0" :group="s.group" kind="BED_ASSIGNMENT" @select="item=>{historySelection=item;execution=item;showExecution=true}"/><AllocationComparison :group="s.group" kind="BED_ASSIGNMENT"/><el-button v-if="execution" size="small" text @click="showExecution=!showExecution">{{showExecution?'收起分析过程':'查看本次分配过程'}}</el-button><AllocationProgress v-if="showExecution" :task="execution" kind="BED_ASSIGNMENT"/><details v-if="historySelection?.status==='completed'"><summary>查看历史候选（只读）</summary><p v-for="[bedId,nurseId] in Object.entries(historySelection.result.assignments||{})" :key="bedId" class="small">{{board.beds.find(b=>String(b.id)===bedId)?.number||'历史床位 #'+bedId}}床：{{nurseName(nurseId)}}</p></details><el-button v-if="oneTimeExperiences.length" size="small" :disabled="!s.canEdit||!board.id||executionBusy" @click="recommend">应用建议并重新优化</el-button><AllocationAssistant :group="s.group" kind="BED_ASSIGNMENT" :execution="execution" :can-edit="s.canEdit" :configured="s.data.ai.configured" :context-ref="execution?.status==='completed'?{execution_id:execution.id}:{}" :selected-experience-ids="oneTimeExperiences" @toggle-once="toggleExperience" @apply-now="applyExperienceNow" @reoptimize="recommend"/></template>
<template v-else><p class="muted small">以下为候选方案的后端校验结果，发布前仍需人工核对。</p><template v-if="recommendation"><ValidationResult :report="recommendation.validation"/><p class="small">已分配 {{recommendation.evaluation?.assigned}} / {{recommendation.evaluation?.patient_count}} 人，未分配 {{recommendation.evaluation?.unassigned}} 人。</p><p class="small">病情评估能力匹配：{{recommendation.evaluation?.assessment_match?.comparable_count||0}} / {{recommendation.evaluation?.assessment_match?.higher_need_count||0}} 名需较高评估支持患者有完整可比较资料；{{recommendation.evaluation?.assessment_match?.unconfirmed_need_count||0}} 人需求待核对。</p><p v-for="row in recommendation.evaluation?.assessment_match?.matches||[]" :key="row.bed_id" class="small">{{board.beds.find(b=>b.id===row.bed_id)?.number||row.bed_id}}床：{{nurseName(row.nurse_id)}} · {{row.assessment_level}}档（1档最高）</p><p v-for="row in recommendation.evaluation?.nurses||[]" :key="row.nurse_id" class="small">{{nurseName(row.nurse_id)}}：{{row.patients}} 床，负荷 {{row.load}}，高负荷患者 {{row.high_load_patients}} 人。</p></template><el-empty v-else description="尚无候选方案" :image-size="60"/></template></section></div></template>
<el-dialog :model-value="dialog==='ai-consent'" title="确认AI床位分析资料来源" width="520px" @close="closeDialog('ai-consent')"><p>系统将向已配置模型发送去标识化的汇总数据；真实资料须先获得院方及服务器端授权。</p><el-radio-group v-model="aiSynthetic"><el-radio :value="true">本次仅使用合成资料</el-radio><el-radio :value="false">涉及真实资料</el-radio></el-radio-group><el-checkbox v-model="aiConsent">已核对资料来源及外发授权</el-checkbox><template #footer><el-button @click="dialog=''">取消</el-button><el-button type="primary" :disabled="aiSynthetic===null||!aiConsent" :loading="executionBusy" @click="startAI">开始分析</el-button></template></el-dialog>
<el-dialog v-model="reauth" title="重新登录当前账号" width="420px" @close="reauthPassword='';reauthError=''">
  <p class="muted small">重新登录不会清空当前输入或已识别结果。</p><el-form @submit.prevent="signInAgain" label-position="top"><el-form-item label="账号"><el-input :model-value="s.user.username" disabled/></el-form-item><el-form-item label="密码"><el-input v-model="reauthPassword" type="password" show-password autocomplete="current-password"/></el-form-item><el-alert v-if="reauthError" :title="reauthError" type="error" :closable="false"/><el-button type="primary" native-type="submit" :loading="reauthBusy">登录</el-button></el-form>
</el-dialog>
<el-dialog :model-value="dialog==='sync'" title="同步当班护士" width="520px" @close="closeDialog('sync')"><p>{{s.date}} · {{shiftName(s.shift)}} · 来源正式排班 #{{board?.source_publication||'—'}}</p><el-table :data="board?.nurses||[]"><el-table-column prop="name" label="护士"/><el-table-column prop="level" label="层级"/><el-table-column label="独立资质"><template #default="{row}">{{row.certifications.map(c=>c.name).join('、')}}</template></el-table-column></el-table><el-alert title="同步会更新当班名单，保留现有责任分配并重新检查冲突。" type="info" :closable="false" class="section-gap"/><template #footer><el-button @click="dialog=''">取消</el-button><el-button type="primary" :disabled="!board?.source_publication" :loading="s.busy" @click="sync">确认同步</el-button></template></el-dialog>
<el-drawer :model-value="dialog==='review'" title="AI病历识别结果 · 待人工核对" size="620px" :close-on-click-modal="false" @close="closeDialog('review')"><template v-if="extraction"><el-alert :title="extraction.mode==='mock'?'当前为本地模拟识别，不代表真实模型准确性。':'模型提取仅作辅助，请核对每项证据。'" type="warning" :closable="false"/><div v-for="record in extraction.records" :key="record.bed_number" class="patient-section"><h3>{{record.bed_number}}床</h3><PatientForm v-model="record.fields"/></div><el-checkbox v-model="confirmed">已逐项核对床号、患者身份、事实及护理负荷</el-checkbox><div class="window-footer"><el-button @click="dialog=''">取消</el-button><el-button type="primary" :disabled="!confirmed" :loading="s.busy" @click="review">确认写入对应床位</el-button></div></template></el-drawer>
<el-dialog :model-value="dialog==='recommend'" title="智能床位分配建议" width="790px" @close="closeDialog('recommend')"><template v-if="recommendation"><el-alert title="方案预览尚未改变当前分配。请核对后应用至草稿。" type="info" :closable="false"/><el-table :data="board.nurses"><el-table-column prop="name" label="责任护士"/><el-table-column label="推荐分管床位"><template #default="{row}">{{occupied.filter(b=>recommendation.assignments[String(b.id)]===row.id).map(b=>b.number+'床').join('、')||'支援'}}</template></el-table-column><el-table-column label="护理负荷"><template #default="{row}">{{occupied.filter(b=>recommendation.assignments[String(b.id)]===row.id).reduce((a,b)=>a+({light:1,medium:2,high:4}[b.patient.load]),0)}}</template></el-table-column><el-table-column label="资质校验"><template #default>通过约束检查</template></el-table-column></el-table><p class="small muted section-gap">{{recommendation.reason}}</p><ValidationResult :report="recommendation.validation"/></template><template #footer><el-button @click="dialog=''">取消</el-button><el-button @click="recommend" :loading="s.busy">重新推荐</el-button><el-button type="primary" :loading="s.busy" @click="applyRecommendation">应用推荐方案</el-button></template></el-dialog>
<el-drawer :model-value="dialog==='adjust'" title="人工调整责任护士" size="500px" @close="closeDialog('adjust')"><el-form label-position="top"><el-form-item label="调整床位"><el-select v-model="adjust.bed"><el-option v-for="b in occupied" :key="b.id" :value="b.id" :label="b.number+'床 · '+b.patient.name"/></el-select></el-form-item><p>当前责任护士：{{nurseName(adjust.assignments?.[String(adjust.bed)])}}</p><el-form-item label="新责任护士"><el-select v-model="adjust.nurse" clearable :disabled="!!adjust.swap"><el-option v-for="n in board?.nurses||[]" :key="n.id" :value="n.id" :label="n.name+' · '+managed(n.id).length+'/3床'"/></el-select></el-form-item><el-form-item label="或与另一床交换责任护士"><el-select v-model="adjust.swap" clearable><el-option v-for="b in occupied.filter(b=>b.id!==adjust.bed)" :key="b.id" :value="b.id" :label="b.number+'床 · '+nurseName(board.assignments[String(b.id)])"/></el-select></el-form-item><el-form-item label="调整原因"><el-select v-model="adjust.reason" filterable allow-create><el-option v-for="reason in ['特殊治疗资格不匹配','护理负荷过高','高护理负荷患者集中','患者病情变化','护士临时不可用','护理连续性需要','护士专科技能更适合其他患者','其他原因']" :key="reason" :value="reason" :label="reason"/></el-select></el-form-item><div class="role-info"><p v-for="n in board?.nurses||[]" :key="n.id" class="small">{{n.name}}：{{managed(n.id).length}}床 → {{Object.values(adjusted).filter(id=>id===n.id).length}}床</p></div></el-form><div class="window-footer"><el-button @click="dialog=''">取消</el-button><el-button type="primary" :disabled="!adjust.nurse&&!adjust.swap" :loading="s.busy" @click="saveAdjust">保存并校验</el-button></div></el-drawer>
<el-dialog :model-value="['validation','publish'].includes(dialog)" :title="dialog==='publish'?'确认并发布床位分配':'分配校验'" width="620px" @close="closeDialog('validation','publish')"><p>{{s.date}} · {{shiftName(s.shift)}} · 在床 {{occupied.length}}人 / 已分配 {{assigned}}人</p><ValidationResult :report="report" @locate="locate"/><el-checkbox v-if="dialog==='publish'&&report?.warnings?.length" v-model="triple">已确认三床组合、护理负荷及独立资质条件</el-checkbox><template #footer><el-button @click="dialog=''">返回</el-button><el-button v-if="dialog==='publish'" type="primary" :disabled="!report?.valid||(!!report?.warnings?.length&&!triple)" :loading="s.busy" @click="publish">确认发布</el-button><el-button v-else type="primary" @click="validate()">重新校验</el-button></template></el-dialog>
<el-drawer :model-value="['patient','details'].includes(dialog)" :title="(patient?.number||'')+'床 · '+(dialog==='patient'?'人工维护患者信息':'患者详情')" size="590px" @close="closeDialog('patient','details')"><template v-if="dialog==='patient'"><PatientForm v-model="edit" allow-assessment-need/><el-checkbox v-model="confirmed">已核对患者信息，确认为合成演示资料</el-checkbox><div class="window-footer"><el-button @click="dialog=''">取消</el-button><el-button type="primary" :disabled="!confirmed" :loading="s.busy" @click="savePatient">保存</el-button></div></template><template v-else><div v-loading="historyLoading" class="section-gap"><h3>患者资料版本</h3><el-select v-model="viewedRevision" clearable placeholder="当前资料" @change="selectPatientRevision"><el-option v-for="version in patientVersions" :key="version.revision" :value="version.revision" :label="`修订 ${version.revision} · ${version.action} · ${version.actor}`"/></el-select><p v-if="viewedRevision" class="muted small">留痕时间：{{new Date(patientVersions.find(v=>v.revision===viewedRevision)?.recorded_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})}}。历史版本只读。</p><p v-else class="muted small">修订 {{edit.revision}}；首次留痕前的更早变更无法还原。</p></div><h2>{{edit.name}} <el-tag>{{loadName(edit.load)}}</el-tag></h2><p class="small">病情评估支持需求：{{({higher:'需较高评估支持',standard:'未标记较高需求'})[edit.assessment_need]||'待核对'}}</p><div v-for="[key,label] in [['diagnoses','确定诊断'],['suspected','疑似诊断'],['treatments','当前治疗'],['historical','既往治疗'],['suggestions','护理建议（推断）'],['evidence','证据及时间状态']]" :key="key" class="notice"><h3>{{label}}</h3><p>{{edit[key]?.join('；')||'暂无'}}</p></div></template></el-drawer>
</template>
<style scoped>
.batch-progress{padding:9px;border:1px solid #dceafa;border-radius:6px;background:#f7fbff;overflow-wrap:anywhere}
.batch-progress p{margin:5px 0;line-height:1.5}.batch-item{padding:4px 0;border-top:1px solid #e6eff8}.batch-error{color:#c34f4f}
.bed-layout{grid-template-columns:minmax(0,1.6fr) minmax(220px,.78fr) minmax(250px,.94fr);align-items:start}
.bed-layout>.card{min-width:0;max-height:clamp(350px,calc(100vh - 370px),900px);overflow-y:auto}
.bed-layout>.card+.card{margin-top:0}
.bed-column .card-title{gap:8px;align-items:baseline;margin-bottom:10px}
.bed-column .card-title .small{text-align:right}
.bed-filter,.nurse-filter{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:12px}
.bed-filter button,.nurse-filter button{border:1px solid #e0ebf7;border-radius:5px;background:#fff;color:#587391;padding:5px 7px;font-size:11px;line-height:1.3}
.bed-filter button.active,.nurse-filter button.active{color:#0869ef;background:#edf5ff;border-color:#a6ccff}
.bed-filter button span{font-weight:600}
.bed-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
.bed-card{min-width:0;min-height:175px;padding:9px;overflow-wrap:anywhere}
.bed-card header{gap:4px}
.bed-card header .icon-button{margin-left:auto}
.bed-card :deep(.bed-drawing){width:62px;height:37px;margin:2px auto}
.bed-card p{line-height:1.4;margin:4px 0}
.bed-card .el-tag{max-width:100%}
.bed-card .unreviewed{color:#b77916}
.bed-nurse{margin-top:6px;padding:5px}
.nurse-column h2{margin-bottom:10px}
.nurse-card{padding:10px;margin-bottom:8px;overflow-wrap:anywhere}
.nurse-card header{margin-bottom:6px}
.nurse-years{font-size:11px;color:#7288a3;margin-left:auto;white-space:nowrap}
.nurse-load{display:flex;align-items:center;gap:8px}
.nurse-load :deep(.el-progress){flex:1;min-width:0;margin:5px 0}
.nurse-load span{white-space:nowrap}
.skill-line{display:block;line-height:1.5}
@media(min-width:1700px){.bed-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:1200px){.bed-layout{grid-template-columns:minmax(0,1.4fr) minmax(195px,.8fr) minmax(220px,1fr);gap:10px}}
</style>
