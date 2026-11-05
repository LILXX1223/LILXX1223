<script setup>
import { computed } from 'vue'
const props=defineProps({task:Object,kind:{type:String,required:true}})
const groupNames=['读取护士资料','核查分组硬性约束','加载优化偏好','DeepSeek分析人员搭配','OR-Tools执行优化求解','计算分组匹配评价','生成推荐分组方案']
const bedNames=['读取患者与护士资料','识别并核查患者护理需求','核查护士资格与工作负荷','DeepSeek分析床位匹配','OR-Tools执行床位优化','计算床位分配评价','生成推荐床位分配方案']
const rows=computed(()=>(props.kind==='GROUP_ASSIGNMENT'?groupNames:bedNames).map((name,index)=>{
  const events=(props.task?.events||[]).filter(e=>e.stage===index+1)
  return {name,stage:index+1,last:events.at(-1),events}
}))
const statuses={running:'执行中',completed:'已完成',failed:'失败',skipped:'已跳过'}
const solverStatuses={optimal:'已证明最优',feasible:'找到可行解',timeout_feasible:'超时但有可行解',
  timeout_no_solution:'超时且无可行解',infeasible:'无可行解',model_invalid:'求解模型无效',unknown:'未找到可行解',
  OPTIMAL:'已证明最优',FEASIBLE:'找到可行解',INFEASIBLE:'无可行解',UNKNOWN:'未找到可行解'}
const completed=computed(()=>rows.value.filter(r=>r.last?.status==='completed').length)
function display(summary){
  if(!summary)return ''
  return Object.entries(summary).filter(([,v])=>v!==null&&v!==undefined&&typeof v!=='object')
    .map(([k,v])=>`${({message:'说明',summary:'AI分析建议（须人工核对）',nurses:'护士人数',patients:'患者人数',participants:'参与人数',excluded:'排除人数',qualified_leaders:'带组资格人数',group_count:'目标小组数',locked:'锁定人数',same_pairs:'同组约束数',avoid_pairs:'避免同组约束数',min_leaders:'每组最低带组资格人数',mentors:'指定带教人数',unassigned:'未分配',revision:'版本',schedule_publication:'正式排班版本',model:'模型',status:'求解状态',elapsed_seconds:'求解耗时（秒）',reason:'原因',eligible_pairs:'符合条件的匹配数',excluded_pairs:'不符合条件的匹配数'})[k]||k}：${k==='status'?(solverStatuses[v]||v):v}`).join('；')
}
function details(summary){return Object.entries(summary||{}).filter(([,v])=>v&&typeof v==='object').map(([k,v])=>{
  const label=({weights:'实际权重',requested_weights:'界面权重',levels:'护理层级分布',years:'ICU年资分布',skills:'专科技能覆盖人数',independent_certifications:'记录的有效独立资格人数',patient_loads:'已核对患者负荷分布',sizes:'各组目标人数',applied_experiences:'应用的经验',excluded_experiences:'未应用的经验',metrics:'评价指标',plans:'求解结果',concerns:'需人工关注',complementarity:'互补建议'})[k]||k
  const value=k==='plans'&&Array.isArray(v)?v.map(p=>`${p.code}：${solverStatuses[p.status]||p.status}${typeof p.seconds==='number'?`（${p.seconds.toFixed(2)}秒）`:''}${p.normalization?' · 参数尺度 '+JSON.stringify(p.normalization):''}`).join('；'):JSON.stringify(v)
  return `${label}：${value}`
}).join('；')}
</script>
<template>
  <div v-if="task" class="allocation-progress">
    <div class="progress-heading"><b>实时分析过程</b><span>{{completed}} / 7 已完成</span></div>
    <ol><li v-for="row in rows" :key="row.stage" :class="row.last?.status||'waiting'"><div><b>{{row.stage}}. {{row.name}}</b><span>{{statuses[row.last?.status]||'等待中'}}</span></div><small v-if="row.last">{{new Date(row.last.at).toLocaleString('zh-CN')}} · {{display(row.last.summary)}}</small><details v-if="row.events.length>1||details(row.last?.summary)"><summary>阶段记录</summary><p v-for="e in row.events" :key="e.id">{{statuses[e.status]||e.status}} · {{display(e.summary)}} {{details(e.summary)}}</p></details></li></ol>
    <p v-if="task.error" class="progress-error">{{task.error}}</p>
  </div>
</template>
<style scoped>
.allocation-progress{background:#f7fbff;border:1px solid #d9eafb;border-radius:8px;padding:12px}.progress-heading,.allocation-progress li>div{display:flex;justify-content:space-between;gap:8px}.progress-heading span,li span{font-size:12px;color:#69809e}.allocation-progress ol{list-style:none;padding:0;margin:8px 0}.allocation-progress li{padding:8px;border-left:3px solid #d8e5f3;margin:3px 0;font-size:12px}.allocation-progress li.running{border-color:#1677ff}.allocation-progress li.completed{border-color:#45b88a}.allocation-progress li.failed{border-color:#e26868}.allocation-progress small,.allocation-progress p{display:block;color:#6c8098;margin:5px 0;overflow-wrap:anywhere}.progress-error{color:#c33!important}
</style>
