<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, streamApi } from '../services/api'
import AllocationProgress from './AllocationProgress.vue'

const props=defineProps({
  group:{type:[String,Number],required:true},
  kind:{type:String,required:true,validator:value=>['GROUP_ASSIGNMENT','BED_ASSIGNMENT'].includes(value)},
  canEdit:{type:Boolean,default:false},
  configured:{type:Boolean,default:false},
  selectedExperienceIds:{type:Array,default:()=>[]},
  contextRef:{type:Object,default:()=>({})},
  execution:{type:Object,default:null},
})
const emit=defineEmits(['toggle-once','apply-now','reoptimize'])
const tab=ref('chat'), messages=ref([]), message=ref(''), synthetic=ref(null), consent=ref(false)
const sessions=ref([]),sessionId=ref(null)
const loading=ref(false), sending=ref(false), loadError=ref(''), sendError=ref(''), scrollBox=ref(null)
const experienceRows=ref([]), feedbackRows=ref([]), sectionLoading=ref(false), sectionError=ref(''), actionBusy=ref(false)
const experienceVersions=ref({}), versionsLoading=ref(null)
const analysisExpanded=ref(false),historicalExecution=ref(null),historyError=ref('')
const shownExecution=computed(()=>historicalExecution.value||props.execution)
const executionCompleted=computed(()=>new Set((shownExecution.value?.events||[]).filter(e=>e.status==='completed').map(e=>e.stage)).size)
const liveSuggestions=computed(()=>(shownExecution.value?.events||[]).find(e=>e.stage===4&&e.status==='completed')?.summary?.pending_suggestions||[])
const evidence=computed(()=>{
  const events=shownExecution.value?.events||[],prepared=events.find(e=>e.stage===3&&e.status==='completed')?.summary||{},solved=events.find(e=>e.stage===5&&e.status==='completed')?.summary
  return {applied:prepared.applied_experiences||[],excluded:prepared.excluded_experiences||[],solved:!!solved,weights:prepared.weights||{}}
})
const executionStatus=computed(()=>({queued:'等待中',running:'执行中',completed:'已完成',failed:'失败',superseded:'已被新任务替代'}[shownExecution.value?.status]||'等待中'))
watch(()=>[props.execution?.id,props.execution?.status],([id,status],[oldId] = [])=>{
  if(id!==oldId)historicalExecution.value=null
  if(id&&(['queued','running'].includes(status)||id!==oldId))analysisExpanded.value=true
},{immediate:true})
watch(()=>props.execution?.events?.filter(e=>e.stage===4&&e.status==='completed').at(-1)?.id,()=>{
  if(tab.value==='experience')loadSection()
})
const entryMode=ref('manual'), aiText=ref(''), editingExperience=ref(null), form=ref({title:'',description:'',target:'',weight:.8})
let version=0, disposed=false
const title=computed(()=>props.kind==='GROUP_ASSIGNMENT'?'AI分组助手':'AI分配助手')
const scopeKey=computed(()=>String(props.group)+':'+props.kind)
const scopeParams=()=>new URLSearchParams({group:String(props.group),kind:props.kind}).toString()
const businessParams=()=>new URLSearchParams({group:String(props.group),business_type:props.kind}).toString()
const targets=computed(()=>props.kind==='GROUP_ASSIGNMENT'
  ? [['level_balance','护理层级均衡'],['years_balance','ICU年资均衡'],['skill_coverage','专科技能互补'],['mentoring_support','带教支持'],['stability','原有分组保留程度'],['assessment_balance','病情评估能级均衡'],['reference_only','仅供分析参考']]
  : [['load_balance','护理工作负荷均衡'],['minimize_three','减少三床分配'],['continuity','护理连续性'],['assessment_match','病情评估能力匹配'],['reference_only','仅供分析参考']])
const targetLabel=target=>targets.value.find(([key])=>key===target)?.[1]||target
const statusLabel=status=>({pending:'待审核',active:'已长期采用',paused:'已暂停',rejected:'未采用'}[status]||status)
const statusType=status=>({pending:'warning',active:'success',paused:'info',rejected:'info'}[status]||'info')
const validMessages=result=>Array.isArray(result?.messages)?result.messages.filter(item=>
  item&&['user','assistant'].includes(item.role)&&typeof item.content==='string'):[]
function time(value){
  if(!value)return ''
  const date=new Date(value)
  return Number.isNaN(date.getTime())?'':new Intl.DateTimeFormat('zh-CN',{
    timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(date)
}
async function scrollBottom(){await nextTick();if(scrollBox.value)scrollBox.value.scrollTop=scrollBox.value.scrollHeight}
async function viewTask(id){
  const key=scopeKey.value;historyError.value=''
  try{const result=await api(`allocation-tasks/${id}/?`+new URLSearchParams({group:String(props.group)}))
    if(disposed||key!==scopeKey.value)return
    if(result.kind!==props.kind)throw new Error('该任务不属于当前业务。')
    historicalExecution.value=result;analysisExpanded.value=true
    await nextTick();scrollBox.value?.scrollTo({top:0,behavior:'smooth'})
  }catch(error){if(!disposed&&key===scopeKey.value)historyError.value=error.message}
}
const suggestionSource=ref(null)
function draftSuggestion(item){
  // Only prepare editable text. A separate source/authorization confirmation
  // and a model call are required before any suggestion can be saved.
  aiText.value=(item.external_content||item.content).slice(0,1000)
  suggestionSource.value=item.id||null;synthetic.value=null;consent.value=false;editingExperience.value=null;entryMode.value='ai';tab.value='experience'
}
function editExperience(item){editingExperience.value=item;entryMode.value='manual';form.value={title:item.title,description:item.description,target:item.target,weight:Number(item.weight)}}
function cancelExperienceEdit(){editingExperience.value=null;form.value={title:'',description:'',target:'',weight:.8}}
async function load(requestedSession=null){
  const current=++version,key=scopeKey.value
  messages.value=[];loading.value=true;loadError.value='';sendError.value=''
  try{
    const data=await api('allocation-assistant/?'+scopeParams()+(requestedSession?'&session_id='+encodeURIComponent(requestedSession):''))
    if(disposed||current!==version||key!==scopeKey.value)return
    if(!Array.isArray(data?.messages))throw new Error('会话数据格式无效。')
    messages.value=validMessages(data);sessions.value=data.sessions||[];sessionId.value=sessions.value.length?data.session_id:null;await scrollBottom()
  }catch(error){if(!disposed&&current===version)loadError.value=error.message}
  finally{if(!disposed&&current===version)loading.value=false}
}
async function newConversation(){
  if(!props.canEdit||sending.value)return
  const current=version,key=scopeKey.value;sending.value=true;sendError.value=''
  try{const result=await api('allocation-assistant/','POST',{group:Number(props.group),kind:props.kind,action:'new'})
    if(disposed||current!==version||key!==scopeKey.value)return
    messages.value=validMessages(result);sessions.value=result.sessions||[];sessionId.value=result.session_id;message.value=''
  }catch(e){if(!disposed&&current===version)sendError.value=e.message}finally{if(!disposed&&current===version)sending.value=false}
}
async function send(){
  if(!props.canEdit||sending.value)return
  if(!props.configured)return ElMessage.warning('AI 服务未配置，请由系统管理员先完成配置。')
  if(synthetic.value===null)return ElMessage.warning('请选择本次消息及相关资料的数据来源。')
  if(!consent.value)return ElMessage.warning('请确认已核对资料及外发授权。')
  const content=message.value.trim()
  if(!content)return ElMessage.warning('请输入问题。')
  if(content.length>500)return ElMessage.warning('单条消息最多 500 字。')
  const current=version,key=scopeKey.value
  sending.value=true;sendError.value=''
  const previous=[...messages.value]
  messages.value=[...previous,{role:'user',content,created_at:new Date().toISOString()},
    {role:'assistant',content:'',created_at:new Date().toISOString()}]
  try{
    const task=props.execution
    const matchesContext=props.kind!=='GROUP_ASSIGNMENT'||!props.contextRef.task_id||task?.result?.grouping_task_id===props.contextRef.task_id
    const result=await streamApi('allocation-assistant/',{
      group:Number(props.group),kind:props.kind,message:content,session_id:sessionId.value,context:props.contextRef,
      execution_id:task&&matchesContext?task.id:null,synthetic:synthetic.value,consent:true,stream:true},chunk=>{
        if(!disposed&&current===version&&key===scopeKey.value){messages.value[messages.value.length-1].content+=chunk;scrollBottom()}
      })
    if(disposed||current!==version||key!==scopeKey.value)return
    if(!Array.isArray(result?.messages))throw new Error('会话响应格式无效，请重新加载核对。')
    messages.value=validMessages(result);sessions.value=result.sessions||[];sessionId.value=result.session_id;message.value='';await scrollBottom()
  }catch(error){
    if(!disposed&&current===version){messages.value=previous;sendError.value=error.message+' 如请求超时，请先重新加载会话，确认记录后再重试。'}
  }finally{if(!disposed&&current===version)sending.value=false}
}
async function loadSection(){
  const current=version,key=scopeKey.value,selected=tab.value
  if(!['experience','feedback'].includes(selected))return
  sectionLoading.value=true;sectionError.value=''
  try{
    const path=selected==='experience'?'experiences/?':'experiences/feedback/?'
    const rows=await api(path+businessParams())
    if(disposed||current!==version||key!==scopeKey.value||selected!==tab.value)return
    if(!Array.isArray(rows))throw new Error('返回数据格式无效。')
    if(selected==='experience')experienceRows.value=rows
    else feedbackRows.value=rows
  }catch(error){if(!disposed&&current===version&&selected===tab.value)sectionError.value=error.message}
  finally{if(!disposed&&current===version&&selected===tab.value)sectionLoading.value=false}
}
function aiAuthorized(){
  if(!props.configured){ElMessage.warning('AI 服务未配置，请由系统管理员先完成配置。');return false}
  if(synthetic.value===null){ElMessage.warning('请选择资料来源。');return false}
  if(!consent.value){ElMessage.warning('请确认资料及外发授权。');return false}
  return true
}
async function addExperience(){
  if(!props.canEdit||actionBusy.value)return
  const key=scopeKey.value
  let path='experiences/',payload={group:Number(props.group),business_type:props.kind}
  if(entryMode.value==='manual'){
    if(!form.value.title.trim()||!form.value.description.trim()||!form.value.target)return ElMessage.warning('请填写名称、说明和优化目标。')
    payload={...payload,...form.value}
    if(editingExperience.value){
      try{await ElMessageBox.confirm('修改会生成待审核版本，原有长期偏好将停止自动应用，须重新确认。','确认修改经验',{type:'warning'})}catch{return}
      path=`experiences/${editingExperience.value.id}/action/`
      payload={...payload,action:'edit',revision:editingExperience.value.revision,confirmed:true}
    }
  }else{
    if(!aiAuthorized())return
    if(!aiText.value.trim())return ElMessage.warning('请输入需要分析的经验要求。')
    path='experiences/suggest/';payload={...payload,text:aiText.value.trim(),synthetic:synthetic.value,consent:true,...(suggestionSource.value?{source_message_id:suggestionSource.value}:{})}
  }
  actionBusy.value=true;sectionError.value=''
  try{
    await api(path,'POST',payload,{timeout:630000})
    if(disposed||key!==scopeKey.value)return
    ElMessage.success('已保存为待审核经验，请核对后选择是否长期采用。')
    if(editingExperience.value&&props.selectedExperienceIds.includes(editingExperience.value.id))emit('toggle-once',editingExperience.value.id)
    cancelExperienceEdit();aiText.value='';suggestionSource.value=null
    await loadSection()
  }catch(error){if(!disposed&&key===scopeKey.value)sectionError.value=error.message}
  finally{if(!disposed&&key===scopeKey.value)actionBusy.value=false}
}
async function loadVersions(item){
  const key=scopeKey.value;versionsLoading.value=item.id;sectionError.value=''
  try{const rows=await api(`experiences/${item.id}/versions/?`+new URLSearchParams({group:String(props.group)}))
    if(!disposed&&key===scopeKey.value)experienceVersions.value={...experienceVersions.value,[item.id]:rows}
  }catch(error){if(!disposed&&key===scopeKey.value)sectionError.value=error.message}
  finally{if(!disposed&&key===scopeKey.value)versionsLoading.value=null}
}
async function changeExperience(item,action,revision=null,reoptimize=false){
  if(!props.canEdit||actionBusy.value)return
  const labels={adopt:'长期采用',pause:'暂停',resume:'启用',reject:'不采用',restore:`恢复到版本 ${revision}`}
  const notice=action==='restore'?'恢复会生成新版本；若该版本为长期采用状态，将影响后续优化。':'此操作将影响后续优化。'
  try{await ElMessageBox.confirm(`确定${labels[action]}“${item.title}”？${notice}已确认的历史方案不变。`,'确认经验状态',{type:'warning'})}catch{return}
  const key=scopeKey.value;actionBusy.value=true;sectionError.value=''
  try{
    await api(`experiences/${item.id}/action/`,'POST',{group:Number(props.group),revision:item.revision,action,confirmed:true,...(action==='restore'?{version:revision}:{})})
    if(disposed||key!==scopeKey.value)return
    if(props.selectedExperienceIds.includes(item.id))emit('toggle-once',item.id)
    ElMessage.success('经验状态已更新。');await loadSection()
    if(experienceVersions.value[item.id])await loadVersions(item)
    if(reoptimize)emit('reoptimize')
  }catch(error){if(!disposed&&key===scopeKey.value)sectionError.value=error.message}
  finally{if(!disposed&&key===scopeKey.value)actionBusy.value=false}
}
async function analyzeFeedback(){
  if(!props.canEdit||actionBusy.value||!aiAuthorized())return
  const key=scopeKey.value;actionBusy.value=true;sectionError.value=''
  try{
    await api('experiences/analyze-feedback/','POST',{group:Number(props.group),business_type:props.kind,synthetic:synthetic.value,consent:true},{timeout:630000})
    if(disposed||key!==scopeKey.value)return
    ElMessage.success('AI 建议已保存为待审核经验。');tab.value='experience';await loadSection()
  }catch(error){if(!disposed&&key===scopeKey.value)sectionError.value=error.message}
  finally{if(!disposed&&key===scopeKey.value)actionBusy.value=false}
}
watch(scopeKey,()=>{tab.value='chat';message.value='';sessionId.value=null;sessions.value=[];synthetic.value=null;consent.value=false;sending.value=false;actionBusy.value=false;historicalExecution.value=null;historyError.value='';experienceRows.value=[];feedbackRows.value=[];experienceVersions.value={};sectionError.value='';cancelExperienceEdit();aiText.value='';load()},{immediate:true})
watch(tab,()=>{if(tab.value!=='chat')loadSection()})
onBeforeUnmount(()=>{disposed=true;version++})
</script>

<template>
  <div class="assistant">
    <div class="assistant-heading">
      <strong>{{title}}</strong>
      <el-tag size="small" :type="configured?'primary':'warning'">{{configured?'AI 服务配置已保存':'AI 服务未配置'}}</el-tag>
    </div>
    <el-tabs v-model="tab" class="assistant-tabs">
      <el-tab-pane label="对话" name="chat"/>
      <el-tab-pane label="经验" name="experience"/>
      <el-tab-pane label="反馈" name="feedback"/>
    </el-tabs>
    <template v-if="tab==='chat'">
      <div class="session-controls"><el-select v-model="sessionId" size="small" placeholder="历史对话" :disabled="loading||sending" @change="value=>load(value)"><el-option v-for="item in sessions" :key="item.id" :value="item.id" :label="item.label+' · '+time(item.created_at)"/></el-select><el-button v-if="canEdit" size="small" :disabled="loading||sending" @click="newConversation">新建对话</el-button></div>
      <el-alert v-if="loadError" :title="'会话加载失败：'+loadError" type="error" :closable="false"/>
      <el-button v-if="loadError" size="small" :loading="loading" @click="load">重新加载会话</el-button>
      <div ref="scrollBox" class="messages" aria-live="polite" v-loading="loading">
        <el-alert v-if="historyError" :title="'任务记录加载失败：'+historyError" type="error" :closable="false"/>
        <div v-if="shownExecution&&shownExecution.kind===kind" class="analysis-row">
          <div class="message-content"><div class="message-meta">{{title}} · {{historicalExecution?'历史':''}}任务 #{{shownExecution.id}}</div>
            <div class="message-bubble"><strong>AI分配思考与分析过程</strong>
              <p class="small">{{executionStatus}} · 阶段 {{shownExecution.stage||'等待'}} · {{executionCompleted}} / 7 已完成 · 思考模式 {{shownExecution.parameters?.ai_config?.thinking||'历史任务未记录'}}</p>
              <el-button size="small" text type="primary" @click="analysisExpanded=!analysisExpanded">{{analysisExpanded?'收起分析详情':'展开分析详情'}}</el-button>
              <el-button v-if="historicalExecution" size="small" text @click="historicalExecution=null">返回当前任务</el-button>
              <AllocationProgress v-if="analysisExpanded" :task="shownExecution" :kind="kind"/>
              <div v-if="liveSuggestions.length" class="section-gap"><b>AI实时优化建议 · 待确认</b><article v-for="item in liveSuggestions" :key="item.id" class="record"><b>{{item.title}}</b><p>{{item.description}}</p><p class="small muted">{{targetLabel(item.target)}}：{{item.current_weight??'未设置'}} → {{item.weight}} · {{item.executable?'软目标，硬约束不变':'仅供参考，无法进入求解'}}</p><el-button v-if="canEdit&&item.executable&&!historicalExecution" size="small" @click="emit('apply-now',item)">本次采用并重新优化</el-button><el-button v-if="canEdit&&item.executable&&!historicalExecution" size="small" :disabled="actionBusy" @click="changeExperience(item,'adopt',null,true)">长期采用并重新优化</el-button><el-button size="small" @click="tab='experience'">在经验中核对状态</el-button></article></div>
            </div></div>
        </div>
        <p v-if="!messages.length&&!loading&&!loadError" class="muted small empty-chat">暂无对话。可询问分配规则或当前方案的核对重点。</p>
        <div v-for="(item,index) in messages" :key="item.id||index" :class="['message-row',item.role]">
          <div class="message-content">
            <div class="message-meta">{{item.role==='user'?'管理人员':title}} <time>{{time(item.created_at)}}</time></div>
            <div class="message-bubble">{{item.content}}</div>
            <el-button v-if="item.execution_id" size="small" text type="primary" @click="viewTask(item.execution_id)">查看关联任务 #{{item.execution_id}}</el-button>
            <el-button v-if="canEdit&&item.role==='assistant'&&!sending&&item.content" size="small" text type="primary" @click="draftSuggestion(item)">整理为待审核建议</el-button>
          </div>
        </div>
      </div>
      <div class="composer">
        <p class="small muted">模型回复仅供辅助，最终方案须由管理人员核对确认。</p>
        <template v-if="canEdit">
          <el-radio-group v-model="synthetic" size="small" :disabled="sending">
            <el-radio :value="true">合成资料</el-radio>
            <el-radio :value="false">真实资料</el-radio>
          </el-radio-group>
          <el-checkbox v-model="consent" :disabled="sending">已核对内容及外发授权</el-checkbox>
          <el-input v-model="message" type="textarea" :rows="3" maxlength="500" show-word-limit
            :disabled="sending" placeholder="输入问题，避免粘贴不必要的个人或病历信息"/>
          <el-alert v-if="sendError" :title="sendError" type="error" :closable="false"/>
          <el-button type="primary" :loading="sending" :disabled="loading||!!loadError||!configured" @click="send">发送</el-button>
        </template>
        <p v-else class="small muted">当前账号可以查看历史对话，发送由授权管理账号操作。</p>
      </div>
    </template>
    <div v-else class="management" v-loading="sectionLoading">
      <el-alert v-if="sectionError" :title="sectionError" type="error" :closable="false"/>
      <el-button v-if="sectionError" size="small" :loading="sectionLoading" @click="loadSection">重新加载核对</el-button>
      <template v-if="tab==='experience'">
        <p class="small muted">仅展示当前大组和当前业务的经验。长期采用须人工确认，不会改变硬性规则及历史方案。</p>
        <details v-if="shownExecution"><summary>本次应用的经验 · 任务 #{{shownExecution.id}}</summary><p class="small muted">第3阶段读取并校验，转换后的参数：{{JSON.stringify(evidence.weights)}}。{{evidence.solved?'第5阶段已完成求解；零权重表示不考虑该目标。':'尚无已完成的求解证据。'}}</p><p v-for="item in evidence.applied" :key="item.id" class="small">#{{item.id}} {{item.title||item.target}} · 修订 {{item.revision}} · {{item.one_time?'本次确认':'长期确认'}} · {{item.target}} {{item.weight}} · {{evidence.solved?(item.zero_weight?'目标权重为0':'已纳入本轮求解'):'已转换，待求解'}}</p><p v-for="item in evidence.excluded" :key="item.id" class="small">#{{item.id}} 未应用：{{item.reason}}</p><p v-if="!evidence.applied.length&&!evidence.excluded.length" class="small muted">本轮未记录经验，或尚未执行至加载阶段。</p></details>
        <div v-if="canEdit" class="entry">
          <p v-if="editingExperience" class="small">正在编辑：{{editingExperience.title}}。保存后需重新审核。</p>
          <el-radio-group v-model="entryMode" size="small">
            <el-radio-button value="manual">手动录入</el-radio-button>
            <el-radio-button value="ai" :disabled="!!editingExperience">AI辅助提炼</el-radio-button>
          </el-radio-group>
          <template v-if="entryMode==='manual'">
            <el-input v-model="form.title" maxlength="160" placeholder="经验名称"/>
            <el-input v-model="form.description" type="textarea" :rows="2" maxlength="1000" placeholder="经验说明"/>
            <el-select v-model="form.target" placeholder="选择软优化目标" @change="value=>{if(value==='reference_only')form.weight=0}">
              <el-option v-for="[key,label] in targets" :key="key" :value="key" :label="label"/>
            </el-select>
            <label class="weight-label">建议权重：{{Number(form.weight).toFixed(1)}}</label>
            <el-slider v-model="form.weight" :min="0" :max="1" :step=".1" :disabled="form.target==='reference_only'" show-stops/>
          </template>
          <template v-else>
            <el-input v-model="aiText" type="textarea" :rows="3" maxlength="1000" show-word-limit placeholder="描述优化偏好，请勿填写姓名、联系方式或病历详情"/>
            <p v-if="suggestionSource" class="small muted">使用已保存的去标识化来源消息提炼。<el-button text size="small" @click="suggestionSource=null">改用当前输入</el-button></p>
            <el-radio-group v-model="synthetic" size="small"><el-radio :value="true">合成资料</el-radio><el-radio :value="false">真实资料</el-radio></el-radio-group>
            <el-checkbox v-model="consent">已核对内容及外发授权</el-checkbox>
          </template>
          <el-button size="small" type="primary" :loading="actionBusy" :disabled="entryMode==='ai'&&!configured" @click="addExperience">{{editingExperience?'保存待审修改':entryMode==='manual'?'保存待审经验':'生成待审建议'}}</el-button>
          <el-button v-if="editingExperience" size="small" :disabled="actionBusy" @click="cancelExperienceEdit">取消编辑</el-button>
        </div>
        <p v-if="!experienceRows.length&&!sectionLoading&&!sectionError" class="small muted">暂无经验记录。</p>
        <article v-for="item in experienceRows" :key="item.id" class="record">
          <div class="record-title"><strong>{{item.title}}</strong><el-tag :type="statusType(item.status)" size="small">{{statusLabel(item.status)}}</el-tag></div>
          <p>{{item.description}}</p>
          <p v-if="item.target==='reference_only'" class="small muted">仅供AI分析参考，尚未形成可执行参数，不会进入求解器。</p>
          <p class="small muted">{{targetLabel(item.target)}}：{{Number(item.weight).toFixed(1)}} · {{item.source==='ai'?'AI建议':'人工录入'}} · {{time(item.created_at)}}</p>
          <p v-if="item.source_details?.source?.message_id" class="small muted">来源消息 {{item.source_details.source.message_id}} · 任务 {{item.source_details.source.execution_id||'未关联'}}；须人工确认。</p>
          <p v-if="item.source_details?.data_basis&&Object.keys(item.source_details.data_basis).length" class="small muted">当时数据依据：{{JSON.stringify(item.source_details.data_basis)}}；原参数：{{JSON.stringify(item.source_details.current_weights||{})}}</p>
          <p v-if="item.source_details?.count" class="small muted">依据：{{item.source_details.count}}条已采纳反馈；原因统计：{{Object.entries(item.source_details.reason_counts||{}).map(([reason,count])=>reason+' '+count+'次').join('、')}}</p>
          <details @toggle="event=>event.target.open&&loadVersions(item)"><summary>版本记录（当前 {{item.revision}}）</summary>
            <p class="small muted">只显示已有完整快照；更早未记录的版本无法还原。</p>
            <p v-if="versionsLoading===item.id" class="small muted">正在加载版本…</p>
            <div v-for="version in experienceVersions[item.id]||[]" :key="version.revision" class="small section-gap">
              版本 {{version.revision}} · {{statusLabel(version.snapshot.status)}} · {{targetLabel(version.snapshot.target)}} {{Number(version.snapshot.weight).toFixed(1)}} · {{time(version.recorded_at)}}
              <el-button v-if="canEdit&&version.revision!==item.revision" size="small" :disabled="actionBusy" @click="changeExperience(item,'restore',version.revision)">恢复此版本</el-button>
            </div>
          </details>
          <div v-if="canEdit" class="record-actions">
            <el-button size="small" :disabled="actionBusy" @click="editExperience(item)">编辑</el-button>
            <el-button v-if="item.status==='pending'&&item.target!=='reference_only'" size="small" :disabled="actionBusy" @click="emit('toggle-once',item.id,item.revision)">{{selectedExperienceIds.includes(item.id)?'取消本次采用':'仅本次采用'}}</el-button><el-button v-if="item.status==='pending'" size="small" type="primary" :disabled="actionBusy" @click="changeExperience(item,'adopt')">长期采用</el-button>
            <el-button v-if="item.status==='pending'&&item.target!=='reference_only'" size="small" :disabled="actionBusy" @click="emit('apply-now',item)">本次采用并重新优化</el-button>
            <el-button v-if="item.status==='pending'&&item.target!=='reference_only'" size="small" :disabled="actionBusy" @click="changeExperience(item,'adopt',null,true)">长期采用并重新优化</el-button>
            <el-button v-if="item.status==='pending'||item.status==='paused'" size="small" :disabled="actionBusy" @click="changeExperience(item,'reject')">不采用</el-button>
            <el-button v-if="item.status==='active'" size="small" :disabled="actionBusy" @click="changeExperience(item,'pause')">暂停</el-button>
            <el-button v-if="item.status==='paused'" size="small" :disabled="actionBusy" @click="changeExperience(item,'resume')">启用</el-button>
          </div>
        </article>
      </template>
      <template v-else>
        <p class="small muted">反馈保留调整前后客观方案及填写的原因；未填写原因不会由系统推断。</p>
        <div v-if="canEdit" class="entry">
          <p class="small muted">AI 仅分析已采纳反馈的原因频次，生成需人工审核的软优化建议。</p>
          <el-radio-group v-model="synthetic" size="small"><el-radio :value="true">合成资料</el-radio><el-radio :value="false">真实资料</el-radio></el-radio-group>
          <el-checkbox v-model="consent">已核对反馈及外发授权</el-checkbox>
          <el-button size="small" type="primary" :loading="actionBusy" :disabled="!configured" @click="analyzeFeedback">AI分析历史反馈</el-button>
        </div>
        <p v-if="!feedbackRows.length&&!sectionLoading&&!sectionError" class="small muted">暂无人工反馈记录。</p>
        <article v-for="item in feedbackRows" :key="item.id" class="record">
          <div class="record-title"><strong>反馈 #{{item.id}}</strong><el-tag size="small" :type="item.adopted?'success':'info'">{{item.adopted?'最终采纳':'未标记采纳'}}</el-tag></div>
          <p>原因：{{item.reason||'未填写'}}</p>
          <p class="small muted">{{item.created_by}} · {{time(item.created_at)}}<template v-if="item.task_reference"> · 任务 {{item.task_reference}}</template></p>
          <details><summary>查看调整前后业务标识</summary><pre>调整前：{{JSON.stringify(item.before)}}<br>调整后：{{JSON.stringify(item.after)}}<br v-if="Object.keys(item.context||{}).length"><template v-if="Object.keys(item.context||{}).length">记录时上下文：{{JSON.stringify(item.context)}}</template></pre></details>
        </article>
      </template>
    </div>
  </div>
</template>

<style scoped>
.assistant{min-height:0;display:flex;flex-direction:column;height:100%;color:#17375e}
.assistant-heading{display:flex;justify-content:space-between;align-items:center;gap:8px}
.assistant-heading strong{font-size:15px}
.session-controls{display:flex;gap:6px;align-items:center}.session-controls :deep(.el-select){min-width:0;flex:1}
.assistant-tabs :deep(.el-tabs__header){margin:0 0 8px}
.assistant-tabs :deep(.el-tabs__item){font-size:13px;padding:0 12px}
.messages{flex:1;min-height:120px;overflow-y:auto;padding:8px 4px}
.empty-chat{text-align:center;margin:20px 8px}
.message-row,.analysis-row{display:flex;margin-bottom:12px}
.message-row.user{justify-content:flex-end}
.message-content{max-width:92%;min-width:0}
.message-meta{font-size:11px;color:#8092a8;margin-bottom:4px}
.message-meta time{margin-left:6px}
.user .message-meta{text-align:right}
.message-bubble{border-radius:9px;background:#eef5fc;padding:9px 11px;font-size:13px;line-height:1.55;white-space:pre-wrap;overflow-wrap:anywhere}
.user .message-bubble{background:#e3efff;color:#155cc1}
.composer{border-top:1px solid #e1ebf5;padding-top:8px;flex-shrink:0}
.composer p{margin:0 0 6px}
.composer :deep(.el-checkbox){height:auto;white-space:normal;margin:4px 0}
.composer :deep(.el-checkbox__label){white-space:normal;font-size:12px}
.composer :deep(.el-radio){margin-right:14px}
.composer .el-button{margin-top:8px}
.composer .el-alert{margin:8px 0 0}
.management{flex:1;min-height:120px;overflow-y:auto;padding:4px}
.management p{margin:0 0 8px;line-height:1.5}
.entry{border:1px solid #dbe9f7;background:#f8fbff;border-radius:8px;padding:10px;margin:8px 0 12px;display:flex;flex-direction:column;align-items:stretch;gap:8px}
.entry :deep(.el-checkbox){height:auto;white-space:normal;margin:0}
.entry :deep(.el-checkbox__label){white-space:normal;font-size:12px}
.entry :deep(.el-radio){margin-right:12px}
.entry .el-button{align-self:flex-start}
.weight-label{font-size:12px;color:#587391}
.record{border:1px solid #dfeaf6;border-radius:7px;padding:9px;margin:8px 0;font-size:12px;overflow-wrap:anywhere}
.record-title{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:5px}
.record-actions{display:flex;flex-wrap:wrap;gap:6px}
.record-actions .el-button+.el-button{margin-left:0}
.record summary{cursor:pointer;color:#0869ef}
.record pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px}
</style>
