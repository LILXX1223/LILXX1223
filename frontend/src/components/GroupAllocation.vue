<script setup>
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, today } from '../services/api'
import { useWorkspace } from '../stores/workspace'
import NurseAvatar from './NurseAvatar.vue'
import AllocationProgress from './AllocationProgress.vue'
import AllocationAssistant from './AllocationAssistant.vue'
import AllocationHistory from './AllocationHistory.vue'
import AllocationComparison from './AllocationComparison.vue'

const props=defineProps({modelValue:Boolean})
const emit=defineEmits(['update:modelValue','changed'])
const s=useWorkspace()
const task=ref(null), busy=ref(false), selectedPlan=ref('A'), assignments=ref({}), leaders=ref({}), history=ref([])
const comparison=ref(false), confirmation=ref(false), editMode=ref(false), finalAck=ref(false)
const synthetic=ref(null), consent=ref(false), groupCount=ref(4), sizes=ref([]), minLeaders=ref(1), juniorYears=ref(3)
const mentors=ref([]), locked=ref({}), effectiveDate=ref(today()), generatedParams=ref(''), lastError=ref('')
const ruleOpen=ref([]), swapFirst=ref(null), swapSecond=ref(null)
const adjustmentReason=ref('')
const samePairs=ref([]),avoidPairs=ref([]),pairFirst=ref(null),pairSecond=ref(null),pairKind=ref('same')
function addPair(){const a=pairFirst.value,b=pairSecond.value;if(!a||!b||a===b)return ElMessage.warning('请选择两名不同的参与护士。');const pair=[a,b].sort((x,y)=>x-y);if([...samePairs.value,...avoidPairs.value].some(v=>v[0]===pair[0]&&v[1]===pair[1]))return ElMessage.warning('该配对已存在，请先删除原约束。');(pairKind.value==='same'?samePairs:avoidPairs).value.push(pair);pairFirst.value=null;pairSecond.value=null}
const oneTimeExperiences=ref([])
const oneTimeRevisions=ref({})
function toggleExperience(id,revision){if(oneTimeExperiences.value.includes(id)){oneTimeExperiences.value=oneTimeExperiences.value.filter(x=>x!==id);const next={...oneTimeRevisions.value};delete next[String(id)];oneTimeRevisions.value=next}else{oneTimeExperiences.value=[...oneTimeExperiences.value,id];oneTimeRevisions.value={...oneTimeRevisions.value,[String(id)]:revision}}}
const rightTab=ref('assistant')
const formalState=ref(null)
const stabilityApplicable=computed(()=>['confirm','restore'].includes(formalState.value?.current_action)&&eligible.value.some(n=>n.subgroup))
const defaultWeights=()=>({level_balance:.8,years_balance:.7,skill_coverage:.9,mentoring_support:.8,stability:.5,assessment_balance:0})
const weights=ref(defaultWeights()), execution=ref(null),showProcess=ref(false)
const explicitWeightTargets=ref([])
function markWeight(key){if(!explicitWeightTargets.value.includes(key))explicitWeightTargets.value=[...explicitWeightTargets.value,key]}
function resetWeights(){weights.value=defaultWeights();explicitWeightTargets.value=Object.keys(defaultWeights())}
const historySelection=ref(null)
let pollTimer=null, pollGeneration=0
const weightNames={level_balance:'护理层级均衡',years_balance:'ICU年资均衡',skill_coverage:'专科技能互补',mentoring_support:'带教支持',stability:'原有分组保留程度',assessment_balance:'病情评估能级均衡'}
const maxGroups=computed(()=>Math.max(1,s.data.group.rules.phases?.length||4))
const eligible=computed(()=>s.data.nurses.filter(n=>n.status==='active'&&n.base_type==='rotation'&&n.effective_from<=today()))
const excluded=computed(()=>s.data.nurses.filter(n=>!eligible.value.some(e=>e.id===n.id)))
const qualifiedLeaders=computed(()=>eligible.value.filter(n=>n.is_leader))
const currentPlan=computed(()=>task.value?.plans?.find(p=>p.code===selectedPlan.value)||null)
const participants=computed(()=>task.value?.participants||[])
const parameterPayload=()=>({group: Number(s.group),group_count:groupCount.value,group_sizes:[...sizes.value],min_leaders:minLeaders.value,
  mentor_ids:[...mentors.value].sort((a,b)=>a-b),locked:Object.fromEntries(Object.entries(locked.value).filter(([,v])=>Number.isInteger(v))),same_pairs:samePairs.value.map(p=>[...p]),avoid_pairs:avoidPairs.value.map(p=>[...p]),effective_date:effectiveDate.value,junior_years:juniorYears.value,weights:{...weights.value},explicit_weight_targets:[...explicitWeightTargets.value],one_time_experience_ids:[...oneTimeExperiences.value],one_time_experience_revisions:{...oneTimeRevisions.value}})
const parameterKey=()=>JSON.stringify(parameterPayload())
const paramsChanged=computed(()=>!!task.value&&generatedParams.value!==parameterKey())
const equalMap=(a,b)=>!!a&&!!b&&Object.keys(a).length===Object.keys(b).length&&Object.entries(a).every(([k,v])=>b[k]===v)
const reviewMatches=computed(()=>!!task.value?.reviewed&&equalMap(task.value.reviewed.assignments,assignments.value)&&equalMap(task.value.reviewed.leaders,leaders.value))
const matchesRecommended=computed(()=>!!currentPlan.value&&equalMap(currentPlan.value.assignments,assignments.value)&&equalMap(currentPlan.value.leaders,leaders.value))
const displayReport=computed(()=>reviewMatches.value?task.value.reviewed:matchesRecommended.value?currentPlan.value:null)
const report=computed(()=>displayReport.value?.validation||null)
const memberGroups=computed(()=>Array.from({length:task.value?.parameters?.group_count||0},(_,i)=>i+1))
const metricLabels={level_balance:'层级类别分布',years_balance:'年资均衡',skill_coverage:'技能覆盖',mentoring_support:'带教支持',stability:'原组稳定',assessment_balance:'病情评估能级均衡',composite:'方案综合指标'}
const metricKeys=['level_balance','years_balance','skill_coverage','mentoring_support','stability','assessment_balance','composite']
const metricDefinitions=computed(()=>task.value?.metric_definitions||{})
const activeNurse=id=>s.data.nurses.find(n=>n.id===id)||participants.value.find(n=>n.id===id)||{id,name:'已变更人员'}
const members=group=>participants.value.filter(n=>assignments.value[String(n.id)]===group)
const leaderOptions=group=>members(group).filter(n=>n.is_leader)
const isLocked=id=>Number.isInteger(task.value?.parameters?.locked?.[String(id)])
const issues=error=>(error.issues||[]).map(x=>x.message||'').filter(Boolean).join('；')

function resetSizes(){const count=eligible.value.length,base=Math.floor(count/groupCount.value),extra=count%groupCount.value;sizes.value=Array.from({length:groupCount.value},(_,i)=>base+(i<extra?1:0))}
function stopPolling(){pollGeneration++;if(pollTimer)clearTimeout(pollTimer);pollTimer=null}
function reset(){stopPolling();formalState.value=null;oneTimeExperiences.value=[];oneTimeRevisions.value={};explicitWeightTargets.value=[];samePairs.value=[];avoidPairs.value=[];pairFirst.value=null;pairSecond.value=null;adjustmentReason.value='';execution.value=null;showProcess.value=false;weights.value=defaultWeights();task.value=null;selectedPlan.value='A';assignments.value={};leaders.value={};history.value=[];comparison.value=false;confirmation.value=false;editMode.value=false;finalAck.value=false;synthetic.value=null;consent.value=false;groupCount.value=maxGroups.value;minLeaders.value=1;juniorYears.value=3;mentors.value=[];locked.value={};ruleOpen.value=[];swapFirst.value=null;swapSecond.value=null;effectiveDate.value=today();generatedParams.value='';lastError.value='';busy.value=false;resetSizes()}
watch(()=>props.modelValue,open=>{if(open){reset();restoreExecution()}else stopPolling()})
watch(()=>s.group,()=>{emit('update:modelValue',false);reset()})
onBeforeUnmount(stopPolling)
watch(groupCount,resetSizes)
function selectPlan(plan){if(!plan)return;selectedPlan.value=plan.code;assignments.value={...plan.assignments};leaders.value={...plan.leaders};history.value=[];editMode.value=false;finalAck.value=false}
function snapshot(){history.value.push({assignments:{...assignments.value},leaders:{...leaders.value}});if(history.value.length>50)history.value.shift()}
function moveNurse(id,group){if(isLocked(id))return ElMessage.warning('该人员已锁定；请先修改锁定规则并重新生成方案。');snapshot();assignments.value={...assignments.value,[String(id)]:group};finalAck.value=false}
function swapNurses(){const a=swapFirst.value,b=swapSecond.value;if(!a||!b||a===b)return ElMessage.warning('请选择两名不同的护士。');if(isLocked(a)||isLocked(b))return ElMessage.warning('锁定人员不能交换，请先解除锁定并重新生成。');const one=assignments.value[String(a)],two=assignments.value[String(b)];if(one===two)return ElMessage.warning('两名护士已在同一小组。');snapshot();assignments.value={...assignments.value,[String(a)]:two,[String(b)]:one};swapFirst.value=null;swapSecond.value=null;finalAck.value=false}
function setLeader(group,id){snapshot();const next={...leaders.value};if(id)next[String(group)]=id;else delete next[String(group)];leaders.value=next;finalAck.value=false}
function undo(){const old=history.value.pop();if(old){assignments.value=old.assignments;leaders.value=old.leaders;finalAck.value=false}}
function restore(){if(currentPlan.value){snapshot();assignments.value={...currentPlan.value.assignments};leaders.value={...currentPlan.value.leaders};finalAck.value=false}}
function realDataConfirmed(){if(synthetic.value===null){ElMessage.warning('请说明参与分析的人员资料是否为合成数据。');return false}if(!consent.value){ElMessage.warning('请确认已核对数据来源及外发授权。');return false}return true}
async function generate(){
  if(!s.data.ai.configured)return ElMessage.warning('AI服务未配置，请由系统管理员先配置真实AI服务。')
  if(!realDataConfirmed())return
  if(!eligible.value.length)return ElMessage.warning('当前没有符合条件的固定轮转护士。')
  if(sizes.value.length!==groupCount.value||sizes.value.some(n=>!Number.isInteger(n)||n<1)||sizes.value.reduce((a,b)=>a+b,0)!==eligible.value.length)return ElMessage.warning('各组人数之和须等于参与人数，且每组至少1人。')
  if(effectiveDate.value!==today())return ElMessage.warning('目前只支持当天生效；未来日期请等待后续版本。')
  const previous=execution.value,active=['queued','running'].includes(previous?.status)
  if(task.value||active){try{await ElMessageBox.confirm(active?'当前任务正在执行，将由新一轮替代；旧结果不会覆盖新方案。':'重新生成将创建新的候选方案，当前未确认调整不会应用到人员档案。','重新生成方案')}catch{return}}
  const data={...parameterPayload(),synthetic:synthetic.value,consent:true},group=s.group,key=parameterKey();busy.value=true;lastError.value=''
  stopPolling()
  try{const result=await api('allocation-tasks/','POST',{...data,kind:'GROUP_ASSIGNMENT',...(previous?.id?{previous_task_id:previous.id}:{}),...(active?{supersede_confirmed:true}:{})});if(s.group!==group||!props.modelValue)return
    execution.value=result;showProcess.value=true;await pollExecution(result.id,group,key)
  }catch(e){lastError.value=e.message+(issues(e)?' '+issues(e):'');ElMessage.error(lastError.value);busy.value=false;if(active)await pollExecution(previous.id,group,'')}
}
function applyExperienceNow(item){if(!oneTimeExperiences.value.includes(item.id))oneTimeExperiences.value=[...oneTimeExperiences.value,item.id];oneTimeRevisions.value={...oneTimeRevisions.value,[String(item.id)]:item.revision};generate()}
async function pollExecution(id,group,key){
  stopPolling();const token=pollGeneration
  async function poll(){
    try{
      const result=await api(`allocation-tasks/${id}/?group=${group}`)
      if(token!==pollGeneration||s.group!==group||!props.modelValue)return
      execution.value=result
      if(result.status==='completed'){
        const proposal=await api(`grouping/${result.result.grouping_task_id}/?group=${group}`)
        if(token!==pollGeneration||s.group!==group||!props.modelValue)return
        if(!key){const p=proposal.parameters;groupCount.value=p.group_count;await Promise.resolve();sizes.value=[...p.group_sizes];minLeaders.value=p.min_leaders;juniorYears.value=p.junior_years;mentors.value=[...p.mentor_ids];locked.value={...p.locked};samePairs.value=p.same_pairs||[];avoidPairs.value=p.avoid_pairs||[];effectiveDate.value=p.effective_date;weights.value={...defaultWeights(),...(p.requested_weights||p.weights)};explicitWeightTargets.value=[...(p.explicit_weight_targets||[])];oneTimeExperiences.value=[...(result.parameters.one_time_experience_ids||[])];oneTimeRevisions.value={...(result.parameters.one_time_experience_revisions||Object.fromEntries((p.applied_experiences||[]).filter(x=>x.one_time).map(x=>[String(x.id),x.revision])))};key=parameterKey()}
        task.value=proposal;generatedParams.value=key;selectPlan(proposal.plans.find(p=>p.code==='A')||proposal.plans[0]);showProcess.value=false;busy.value=false
        ElMessage.success('真实AI分析和约束求解已完成，请人工核对。');return
      }
      if(['failed','superseded'].includes(result.status)){lastError.value=result.error||'任务失败。';busy.value=false;return}
      pollTimer=setTimeout(poll,1000)
    }catch(e){if(token===pollGeneration){lastError.value='无法获取任务状态：'+e.message;busy.value=false}}
  }
  await poll()
}
async function restoreExecution(){
  const group=s.group
  try{const [list,formal]=await Promise.all([api(`allocation-tasks/?group=${group}&kind=GROUP_ASSIGNMENT`),api(`grouping/formal/?group=${group}`)])
    if(s.group===group&&props.modelValue)formalState.value=formal
    if(s.group!==group||!props.modelValue||!list.length)return
    const latest=list[0];execution.value=latest
    if(['queued','running','completed'].includes(latest.status)){busy.value=true;showProcess.value=latest.status!=='completed';await pollExecution(latest.id,group,'')}
  }catch{/* 仍可使用页面，待下次读取恢复 */}
}
function showHistory(item){historySelection.value=item;execution.value=item;showProcess.value=true}
async function loadHistoryCandidate(){if(!historySelection.value?.result?.grouping_task_id||busy.value)return
  if(task.value&&!equalMap(currentPlan.value?.assignments,assignments.value)){try{await ElMessageBox.confirm('加载历史候选会替换当前窗口中未确认的微调，继续？','加载历史候选')}catch{return}}
  busy.value=true;await pollExecution(historySelection.value.id,s.group,'')
}
async function validate(aiReview=false){
  if(!task.value)return false
  if(paramsChanged.value){ElMessage.warning('分组规则或锁定人员已变更，请重新生成方案。');return false}
  if(aiReview&&!realDataConfirmed())return false
  const group=s.group,id=task.value.id,revision=task.value.revision
  const data={group:Number(group),revision,assignments:{...assignments.value},leaders:{...leaders.value},synthetic:synthetic.value,consent:consent.value,ai_review:aiReview,
    reference_assignments:currentPlan.value?{...currentPlan.value.assignments}:null,reference_leaders:currentPlan.value?{...currentPlan.value.leaders}:null,adjustment_reason:adjustmentReason.value}
  busy.value=true;lastError.value=''
  try{const result=await api(`grouping/${id}/review/`,'POST',data,{timeout:630000});if(s.group!==group||!props.modelValue)return false;task.value=result
    if(!result.reviewed?.validation?.valid){ElMessage.warning('方案尚有阻断性问题，请调整后重新校验。');return false}
    ElMessage.success(aiReview?'已完成后端校验和真实AI重新评价。':'方案已通过后端校验。');return true
  }catch(e){lastError.value=e.message+(issues(e)?' '+issues(e):'');ElMessage.error(lastError.value);return false}finally{busy.value=false}
}
async function openConfirmation(){if(await validate(false)){finalAck.value=false;confirmation.value=true}}
async function confirm(){
  if(!task.value||!finalAck.value||!reviewMatches.value||!task.value.reviewed.validation.valid||paramsChanged.value)return
  const group=s.group,id=task.value.id,data={group:Number(group),revision:task.value.revision,assignments:{...assignments.value},leaders:{...leaders.value},confirmed:true}
  busy.value=true;lastError.value=''
  try{const result=await api(`grouping/${id}/confirm/`,'POST',data);if(s.group!==group)return;task.value=result;confirmation.value=false;emit('changed');try{await s.load(true)}catch{ElMessage.warning('分组已确认，但页面刷新失败，请手动刷新核对。')}ElMessage.success('小组分配已确认，已发布历史排班保持原样。');emit('update:modelValue',false)}
  catch(e){lastError.value=e.message+(issues(e)?' '+issues(e):'');ElMessage.error(lastError.value)}finally{busy.value=false}
}
const movedCount=computed(()=>participants.value.filter(n=>n.subgroup!==assignments.value[String(n.id)]).length)
function scoreText(value){return value===null||value===undefined?'暂不可计算':`${Number(value).toFixed(1)} / 100`}
</script>

<template>
<el-dialog :model-value="modelValue" title="AI辅助小组分配" width="min(1500px,96vw)" top="4vh" class="allocation-dialog" :close-on-click-modal="false" @close="emit('update:modelValue',false)">
  <div class="allocation-top"><p class="muted small">读取本组最新护士档案。AI提供资料分析，OR-Tools检查硬约束；负责人确认前不会改变正式分组。</p><el-tag :type="s.data.ai.configured?'primary':'warning'">{{s.data.ai.configured?'已保存 AI 服务配置':'AI服务未配置'}}</el-tag></div>
  <el-alert v-if="!s.data.ai.configured" title="AI服务未配置。请联系系统管理员配置并测试真实服务后再生成方案。" type="warning" :closable="false"/>
  <el-alert v-if="lastError" :title="lastError" type="error" :closable="false"/>
  <el-alert v-if="paramsChanged" title="规则、参与配置或锁定人员已修改；当前方案不能确认，请重新生成。" type="warning" :closable="false"/>
  <div class="allocation-layout">
    <section class="panel rule-panel"><h3>分组规则</h3>
      <div class="rule-summary"><div><b>{{eligible.length}}</b><small>参与固定轮转</small></div><div><b>{{excluded.length}}</b><small>暂不参与</small></div><div><b>{{qualifiedLeaders.length}}</b><small>具备带组资格</small></div></div>
      <ol class="rule-list"><li>在岗、已生效的固定轮转护士参加</li><li>长期固定白班及非在岗人员不进入轮转</li><li>按各组目标人数分配</li><li>按带组资格检查人数及实际带组老师</li><li>比较护理层级类别和年资分布</li><li>比较有效专科技能覆盖</li><li>结合人工指定的带教人员</li><li>参考原有小组稳定性</li><li>锁定人员并允许负责人调整</li></ol>
      <el-form label-position="top"><el-form-item label="小组数量"><el-input-number v-model="groupCount" :min="1" :max="maxGroups" size="small"/></el-form-item>
        <el-form-item v-for="(size,i) in sizes" :key="i" :label="'第'+(i+1)+'小组目标人数'"><el-input-number v-model="sizes[i]" :min="1" :max="eligible.length||1" size="small"/></el-form-item>
        <el-form-item label="每组最低带组资格人数"><el-input-number v-model="minLeaders" :min="0" :max="eligible.length||1" size="small"/></el-form-item>
        <el-form-item label="低年资界限（ICU年数≤）"><el-input-number v-model="juniorYears" :min="0" :max="60" size="small"/><small class="muted">默认3年仅作演示，须由科室确认。</small></el-form-item>
        <el-form-item label="可承担带教的护士（由管理者指定）"><el-select v-model="mentors" multiple filterable collapse-tags placeholder="按实际情况选择"><el-option v-for="n in eligible" :key="n.id" :value="n.id" :label="n.name+' · '+n.employee_id"/></el-select></el-form-item>
        <el-form-item label="生效日期"><el-date-picker v-model="effectiveDate" value-format="YYYY-MM-DD" :clearable="false" :disabled-date="d=>d.toLocaleDateString('sv-SE')!==today()"/></el-form-item>
        <p class="muted small">目前仅支持当天生效。病情评估能级1档最高；缺失能级不推断。均衡目标默认关闭，须由管理人员选择。</p>
        <el-collapse v-model="ruleOpen"><el-collapse-item title="优化偏好（仅软约束）" name="weights"><div v-for="(label,key) in weightNames" :key="key" class="weight-row"><span>{{label}} · {{key==='stability'&&!stabilityApplicable?'不适用':Number(weights[key]).toFixed(1)}}</span><el-slider v-model="weights[key]" :min="0" :max="1" :step="0.1" :disabled="key==='stability'&&!stabilityApplicable" @change="markWeight(key)"/></div><p class="muted small">本次明确调整的滑块优先于长期偏好；其余目标应用有效长期经验。无需加总为1。</p><el-button size="small" @click="resetWeights">恢复默认权重</el-button></el-collapse-item><el-collapse-item :title="'锁定人员（'+Object.values(locked).filter(v=>Number.isInteger(v)).length+'人）'" name="lock"><p class="muted small">选择需要固定的组；清空即可解除锁定。更改锁定后须重新生成。</p><div v-for="n in eligible" :key="n.id" class="lock-row"><span>{{n.name}} <small>{{n.employee_id}}</small></span><el-select v-model="locked[String(n.id)]" clearable placeholder="未锁定" size="small"><el-option v-for="i in groupCount" :key="i" :value="i" :label="'第'+i+'小组'"/></el-select></div></el-collapse-item>
        <el-collapse-item title="指定同组或避免同组" name="pairs"><el-radio-group v-model="pairKind" size="small"><el-radio value="same">指定同组</el-radio><el-radio value="avoid">避免同组</el-radio></el-radio-group><el-select v-model="pairFirst" filterable placeholder="第一名护士"><el-option v-for="n in eligible" :key="n.id" :value="n.id" :label="n.name"/></el-select><el-select v-model="pairSecond" filterable placeholder="第二名护士"><el-option v-for="n in eligible" :key="n.id" :value="n.id" :label="n.name"/></el-select><el-button size="small" @click="addPair">添加约束</el-button><div v-for="(pair,i) in samePairs" :key="'same'+i" class="lock-row small"><span>同组：{{activeNurse(pair[0]).name}} / {{activeNurse(pair[1]).name}}</span><el-button link type="danger" @click="samePairs.splice(i,1)">删除</el-button></div><div v-for="(pair,i) in avoidPairs" :key="'avoid'+i" class="lock-row small"><span>避免同组：{{activeNurse(pair[0]).name}} / {{activeNurse(pair[1]).name}}</span><el-button link type="danger" @click="avoidPairs.splice(i,1)">删除</el-button></div></el-collapse-item><el-collapse-item :title="'不参与名单（'+excluded.length+'人）'" name="excluded"><div v-for="n in excluded" :key="n.id" class="small lock-row"><span>{{n.name}}</span><span class="muted">{{n.base_type==='day'?'长期固定白班':n.status!=='active'?'非在岗':'尚未生效'}}</span></div></el-collapse-item></el-collapse>
        <el-form-item label="人员资料来源" class="section-gap"><el-radio-group v-model="synthetic"><el-radio :value="true">合成资料</el-radio><el-radio :value="false">真实人员资料</el-radio></el-radio-group></el-form-item><el-checkbox v-model="consent">已核对资料来源并确认符合外发要求</el-checkbox><p class="muted small">真实人员资料必须另有医院授权；仅勾选确认不代表授权已完成。</p>
      </el-form>
    </section>
    <section class="panel plans-panel"><div class="card-title"><h3>{{showProcess?'实时分组分析':'推荐分组'}}</h3><el-button v-if="execution" size="small" text @click="showProcess=!showProcess">{{showProcess?'查看推荐方案':'查看优化过程'}}</el-button><el-tag v-if="task" type="success">任务 #{{task.id}} · 修订 {{task.revision}}</el-tag></div><AllocationHistory :key="execution?.id||0" :group="s.group" kind="GROUP_ASSIGNMENT" @select="showHistory"/><AllocationComparison :group="s.group" kind="GROUP_ASSIGNMENT"/><el-button v-if="showProcess&&historySelection?.status==='completed'" size="small" :disabled="busy" @click="loadHistoryCandidate">加载历史候选（不修改正式分组）</el-button>
      <AllocationProgress v-if="showProcess&&execution" :task="execution" kind="GROUP_ASSIGNMENT"/>
      <el-empty v-else-if="!task" description="请配置规则并生成方案。" :image-size="80"/>
      <template v-else><div class="plan-tabs"><el-button v-for="plan in task.plans" :key="plan.code" :type="selectedPlan===plan.code?'primary':'default'" :disabled="plan.applicable===false" size="small" @click="selectPlan(plan)">方案{{plan.code}} · {{plan.name}}{{plan.applicable===false?'（不适用）':''}}</el-button></div>
        <p class="muted small">{{editMode?'可调整成员所属小组和实际带组老师，之后请重新校验。':'点击“人工微调”后调整；锁定人员需先解除锁定并重新生成。'}}</p>
        <div v-if="editMode" class="swap-controls"><el-select v-model="swapFirst" filterable placeholder="选择第一名护士" size="small"><el-option v-for="n in participants" :key="n.id" :value="n.id" :label="n.name"/></el-select><el-select v-model="swapSecond" filterable placeholder="选择第二名护士" size="small"><el-option v-for="n in participants" :key="n.id" :value="n.id" :label="n.name"/></el-select><el-button size="small" @click="swapNurses">交换两人</el-button></div><el-select v-if="editMode" v-model="adjustmentReason" clearable filterable allow-create placeholder="可选择或填写调整原因"><el-option v-for="reason in ['层级配置不合理','ICU年资搭配不合理','专科技能覆盖不足','低年资护士集中','带教支持不足','带组老师配置不合理','希望保留既有人员组合','临时人员因素','其他原因']" :key="reason" :value="reason" :label="reason"/></el-select>
        <div class="groups-grid"><article v-for="group in memberGroups" :key="group" class="group-card" :class="'group-color-'+((group-1)%4)"><header><b>第{{group}}小组</b><small>{{members(group).length}} / {{task.parameters.group_sizes[group-1]}} 人</small></header>
          <div class="leader-line"><span>实际带组老师</span><el-select v-if="editMode" :model-value="leaders[String(group)]" clearable placeholder="请选择" size="small" @update:model-value="id=>setLeader(group,id)"><el-option v-for="n in leaderOptions(group)" :key="n.id" :value="n.id" :label="n.name"/></el-select><b v-else>{{leaders[String(group)]?activeNurse(leaders[String(group)]).name:'未指定'}}</b></div>
          <div v-for="n in members(group)" :key="n.id" class="member"><NurseAvatar :nurse="activeNurse(n.id)" tiny/><div class="member-body"><b>{{n.name}}</b><small>{{n.level}} · ICU {{n.years}}年 <span v-if="isLocked(n.id)">· 已锁定</span></small><small>{{n.skills?.join('、')||'未记录专科技能'}}</small></div><el-select v-if="editMode" :model-value="assignments[String(n.id)]" :disabled="isLocked(n.id)" size="small" @update:model-value="value=>moveNurse(n.id,value)"><el-option v-for="target in memberGroups" :key="target" :value="target" :label="'第'+target+'组'"/></el-select></div>
        </article></div>
        <div v-if="report" class="section-gap"><el-alert :title="report.valid?'当前显示方案通过约束校验':'当前显示方案存在阻断性问题'" :type="report.valid?'success':'error'" :closable="false"/><div v-for="(item,i) in report.errors||[]" :key="'error'+i" class="issue error">{{item.message}}</div><div v-for="(item,i) in report.warnings||[]" :key="'warn'+i" class="issue warning">{{item.message}}</div></div><el-alert v-else class="section-gap" title="人工调整后尚未校验，不能沿用原方案的校验结果与评分。" type="warning" :closable="false"/>
      </template>
    </section>
    <section class="panel evaluation-panel"><el-tabs v-model="rightTab"><el-tab-pane label="AI分组助手" name="assistant"/><el-tab-pane label="匹配评价" name="evaluation"/></el-tabs><AllocationAssistant v-if="rightTab==='assistant'" :group="s.group" kind="GROUP_ASSIGNMENT" :execution="execution" :can-edit="s.canEdit" :configured="s.data.ai.configured" :context-ref="task?{task_id:task.id,plan_code:selectedPlan}:{}" :selected-experience-ids="oneTimeExperiences" @toggle-once="toggleExperience" @apply-now="applyExperienceNow" @reoptimize="generate"/><template v-else><h3>匹配评价</h3><el-empty v-if="!task" description="等待真实AI分析与求解" :image-size="70"/>
      <template v-else><div v-for="key in metricKeys" :key="key" class="metric-row"><div><span>{{metricLabels[key]}}</span><b>{{scoreText(displayReport?.metrics?.[key])}}</b></div><el-progress v-if="displayReport?.metrics?.[key]!==null&&displayReport?.metrics?.[key]!==undefined" :percentage="Math.max(0,Math.min(100,Number(displayReport.metrics[key])))" :show-text="false"/><p v-if="metricDefinitions[key]" class="muted small">{{metricDefinitions[key]}}</p></div>
        <p class="muted small">指标仅比较方案的已记录信息，并非个人临床能力评分。</p>
        <h3>AI推荐依据</h3><div v-for="(line,i) in task.ai_analysis?.complementarity||[]" :key="'c'+i" class="analysis-line">{{line}}</div><div v-for="(line,i) in task.ai_analysis?.teaching||[]" :key="'t'+i" class="analysis-line">{{line}}</div><h3>需要人工关注</h3><div v-if="!task.ai_analysis?.concerns?.length" class="muted small">AI未返回额外提示，仍须逐项核对。</div><div v-for="(line,i) in task.ai_analysis?.concerns||[]" :key="'a'+i" class="issue warning">{{line}}</div>
        <el-collapse><el-collapse-item title="逐人能力分析摘要" name="analyses"><div v-for="item in task.ai_analysis?.analyses||[]" :key="item.nurse_id" class="analysis-line"><b>{{activeNurse(item.nurse_id).name}}：</b>{{item.summary}}</div></el-collapse-item></el-collapse>
        <div v-if="task.reviewed?.ai_review" class="role-info section-gap"><b>人工调整后的AI评价</b><p>{{task.reviewed.ai_review}}</p></div>
        <el-button v-if="editMode" :disabled="paramsChanged" :loading="busy" @click="validate(true)">真实AI重新评价</el-button>
      </template></template>
    </section>
  </div>
  <template #footer><div class="allocation-footer"><el-button @click="emit('update:modelValue',false)">取消</el-button><el-button :disabled="!s.canEdit||!s.data.ai.configured" :loading="busy" @click="generate">{{task?'重新生成':'生成方案'}}</el-button><el-button v-if="oneTimeExperiences.length" :disabled="!s.canEdit||!s.data.ai.configured||busy" @click="generate">应用建议并重新优化</el-button><el-button @click="ruleOpen=['lock']">锁定人员</el-button><el-button :disabled="!task" @click="editMode=!editMode">{{editMode?'结束微调':'人工微调'}}</el-button><el-button :disabled="!task||!history.length" @click="undo">撤销本次调整</el-button><el-button :disabled="!task" @click="restore">恢复推荐方案</el-button><el-button :disabled="!task" @click="comparison=true">方案对比</el-button><el-button type="primary" :disabled="!task||paramsChanged||!s.canEdit" :loading="busy" @click="openConfirmation">确认分组</el-button></div></template>
</el-dialog>
<el-dialog v-model="comparison" title="候选分组方案对比" width="min(900px,92vw)" append-to-body><p class="muted small">数据来自同一批真实AI分析和约束求解结果；指标口径见分组主窗口。</p><div class="comparison-grid"><article v-for="plan in task?.plans||[]" :key="plan.code" class="panel"><h3>方案{{plan.code}} · {{plan.name}}{{plan.applicable===false?'（不适用）':''}}</h3><p v-for="key in metricKeys" :key="key" class="small">{{metricLabels[key]}}：<b>{{scoreText(plan.metrics?.[key])}}</b></p><el-button :disabled="plan.applicable===false" @click="selectPlan(plan);comparison=false">选择此方案</el-button></article></div><template #footer><el-button @click="comparison=false">关闭</el-button></template></el-dialog>
<el-dialog v-model="confirmation" title="确认应用小组分配" width="min(760px,92vw)" append-to-body><p>生效日期：{{effectiveDate}} · 将调整 {{movedCount}} 名护士的小组归属。</p><el-alert title="原有四天轮转规则保持不变；已发布历史排班不会自动修改，后续草稿需要重新核对。" type="info" :closable="false"/><div v-for="group in memberGroups" :key="group" class="confirmation-group"><b>第{{group}}小组：</b>{{members(group).map(n=>n.name).join('、')||'无'}} <span class="muted">· 带组：{{leaders[String(group)]?activeNurse(leaders[String(group)]).name:'未指定'}}</span></div><h4>规则校验</h4><p class="small">参与范围、小组人数、带组资格、锁定人员及其他后端约束已执行；方案指标和AI建议仍须人工判断。</p><div v-for="(item,i) in task?.reviewed?.validation?.warnings||[]" :key="i" class="issue warning">{{item.message}}</div><el-checkbox v-model="finalAck">已核对最终名单、带组老师与对后续排班的影响</el-checkbox><template #footer><el-button @click="confirmation=false">返回调整</el-button><el-button type="primary" :disabled="!finalAck||!reviewMatches" :loading="busy" @click="confirm">确认应用</el-button></template></el-dialog>
</template>

<style scoped>
.allocation-top{display:flex;justify-content:space-between;gap:14px;align-items:center}.allocation-top p{margin:0 0 12px}.allocation-layout{display:grid;grid-template-columns:minmax(240px,.9fr) minmax(430px,1.8fr) minmax(230px,.85fr);gap:12px;align-items:start}.panel{background:#fff;border:1px solid #deebf8;border-radius:8px;padding:14px;min-width:0}.panel h3{font-size:15px;margin:0 0 12px}.panel .card-title{margin-bottom:8px}.panel .card-title h3{margin:0}.rule-summary{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:10px}.rule-summary div{background:#edf6ff;text-align:center;padding:8px 4px;border-radius:6px}.rule-summary b{display:block;color:#0869ef;font-size:18px}.rule-summary small{font-size:10px;color:#738ba8}.rule-list{font-size:12px;color:#4b6481;padding-left:20px;line-height:1.65}.rule-panel .el-form-item{margin-bottom:10px}.rule-panel .el-select,.rule-panel .el-date-editor{width:100%}.lock-row{display:flex;justify-content:space-between;gap:8px;align-items:center;padding:5px 0}.lock-row small{color:#8ca0b6}.lock-row .el-select{width:110px}.plan-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}.plan-tabs .el-button+.el-button{margin-left:0}.swap-controls{display:flex;gap:6px;margin-bottom:10px}.swap-controls .el-select{min-width:0;flex:1}.groups-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.group-card{border:1px solid #e0e9f3;background:#f7fbff;border-radius:8px;padding:10px}.group-color-0{background:#fff8ee}.group-color-1{background:#f8f3ff}.group-color-2{background:#f1fbf6}.group-color-3{background:#f1f8ff}.group-card header,.leader-line{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:12px}.group-card header{padding-bottom:8px;border-bottom:1px solid #dfe8f2}.group-card header small{color:#768ba4}.leader-line{margin:8px 0;color:#57718e}.leader-line .el-select{width:115px}.member{display:flex;align-items:center;gap:6px;padding:7px 0;border-top:1px solid #e7edf5}.member-body{min-width:0;flex:1}.member-body b,.member-body small{display:block}.member-body b{font-size:12px}.member-body small{font-size:10px;color:#7186a0;overflow-wrap:anywhere}.member .el-select{width:83px}.metric-row{margin-bottom:12px}.metric-row>div{display:flex;justify-content:space-between;gap:8px;font-size:12px;margin-bottom:5px}.metric-row p{margin:4px 0}.analysis-line{font-size:12px;color:#496682;padding:8px 0;border-bottom:1px solid #edf3fa;line-height:1.6}.evaluation-panel h3:not(:first-child){margin-top:17px}.evaluation-panel .role-info{padding:10px;font-size:12px}.evaluation-panel .role-info p{margin:5px 0}.allocation-footer{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap}.allocation-footer .el-button+.el-button{margin-left:0}.comparison-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.confirmation-group{font-size:13px;padding:8px;border-bottom:1px solid #edf3fa}@media(max-width:1280px){.allocation-layout{grid-template-columns:220px minmax(350px,1fr) 220px}.groups-grid{grid-template-columns:1fr}}
:global(.allocation-dialog){display:flex;flex-direction:column;height:92vh;max-height:92vh}:global(.allocation-dialog .el-dialog__body){display:flex;flex-direction:column;flex:1;min-height:0;max-height:none;overflow:hidden}.allocation-layout{flex:1;min-height:0;align-items:stretch}.allocation-layout>.panel{min-height:0;overflow-y:auto}.evaluation-panel .assistant{height:calc(100% - 45px);min-height:300px}
</style>
