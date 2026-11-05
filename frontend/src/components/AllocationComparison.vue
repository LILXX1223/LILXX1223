<script setup>
import { ref, watch } from 'vue'
import { api } from '../services/api'
const props=defineProps({group:[String,Number],kind:String})
const open=ref(false),tasks=ref([]),left=ref(null),right=ref(null),leftPlan=ref('A'),rightPlan=ref('A'),result=ref(null),error=ref(''),busy=ref(false)
async function show(){open.value=true;error.value='';result.value=null
  try{tasks.value=(await api(`allocation-tasks/?group=${props.group}&kind=${props.kind}`)).filter(t=>t.status==='completed')
    right.value=tasks.value[0]?.id||null;left.value=tasks.value[1]?.id||right.value
  }catch(e){error.value=e.message}}
async function compare(){if(!left.value||!right.value)return;busy.value=true;error.value='';result.value=null
  try{result.value=await api('allocation-tasks/compare/?'+new URLSearchParams({group:String(props.group),left:String(left.value),right:String(right.value),left_plan:leftPlan.value,right_plan:rightPlan.value}))}
  catch(e){error.value=e.message}finally{busy.value=false}}
watch(()=>[props.group,props.kind],()=>{open.value=false;result.value=null})
</script>
<template><el-button size="small" @click="show">优化前后对比</el-button><el-dialog v-model="open" title="多轮优化对比" width="min(900px,92vw)" append-to-body>
  <p class="small muted">比较同一数据、硬性规则和评价口径下的候选方案；不同权重下的求解目标分数不作比较。</p>
  <div class="compare-select"><el-select v-model="left" placeholder="选择之前任务"><el-option v-for="t in tasks" :key="t.id" :value="t.id" :label="'#'+t.id+' · '+new Date(t.created_at).toLocaleString('zh-CN')"/></el-select><span>→</span><el-select v-model="right" placeholder="选择之后任务"><el-option v-for="t in tasks" :key="t.id" :value="t.id" :label="'#'+t.id+' · '+new Date(t.created_at).toLocaleString('zh-CN')"/></el-select><el-button type="primary" :loading="busy" @click="compare">比较</el-button></div>
  <div v-if="kind==='GROUP_ASSIGNMENT'" class="compare-select"><span>之前方案</span><el-select v-model="leftPlan"><el-option v-for="code in ['A','B','C']" :key="code" :value="code" :label="'方案 '+code"/></el-select><span>之后方案</span><el-select v-model="rightPlan"><el-option v-for="code in ['A','B','C']" :key="code" :value="code" :label="'方案 '+code"/></el-select></div>
  <el-alert v-if="error" :title="error" type="warning" :closable="false"/>
  <template v-if="result"><p class="small">{{result.note}}</p><p class="small">分配变化 {{result.changed.length}} 项；比较的是候选方案，正式分组和正式责任护士未改变。</p><div class="compare-rows"><section v-for="row in result.rounds" :key="row.task_id" class="compare-panel"><b>任务 #{{row.task_id}} · {{row.solver}}</b><p class="small">硬性约束：{{row.constraints_valid?'通过':'待核对'}}</p><p class="small">本轮权重：{{JSON.stringify(row.weights)}}</p><p class="small">真实评价：{{JSON.stringify(row.metrics)}}</p></section></div><details><summary>变化明细（稳定ID）</summary><p v-for="item in result.changed" :key="item.id" class="small">{{item.id}}：{{item.before??'未分配'}} → {{item.after??'未分配'}}</p></details></template>
  <template #footer><el-button @click="open=false">关闭</el-button></template></el-dialog></template>
<style scoped>.compare-select,.compare-rows{display:flex;gap:10px;align-items:center;margin:12px 0}.compare-select .el-select{flex:1}.compare-rows{align-items:stretch}.compare-panel{flex:1;min-width:0;background:#f6faff;padding:12px;border-radius:8px;overflow-wrap:anywhere}</style>
