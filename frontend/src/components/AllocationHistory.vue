<script setup>
import { onMounted, ref, watch } from 'vue'
import { api } from '../services/api'
const props=defineProps({group:[String,Number],kind:String})
const emit=defineEmits(['select'])
const records=ref([]),selected=ref(null),error=ref('')
let sequence=0
async function load(){const id=++sequence;error.value=''
  try{const list=await api(`allocation-tasks/?group=${props.group}&kind=${props.kind}`);if(id===sequence)records.value=list}
  catch(e){if(id===sequence)error.value=e.message}
}
function choose(id){const item=records.value.find(row=>row.id===id);if(item)emit('select',item)}
watch(()=>[props.group,props.kind],load)
onMounted(load)
</script>
<template><div class="allocation-history"><el-select v-model="selected" size="small" clearable placeholder="查看历史任务" @change="choose"><el-option v-for="row in records" :key="row.id" :value="row.id" :label="'#'+row.id+' · '+({completed:'已完成',failed:'失败',running:'执行中',queued:'等待中'}[row.status]||row.status)+' · '+new Date(row.created_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})"/></el-select><el-button size="small" text @click="load">刷新</el-button><small v-if="error" class="muted">{{error}}</small></div></template>
<style scoped>.allocation-history{display:flex;align-items:center;gap:4px;max-width:100%;margin:4px 0}.allocation-history :deep(.el-select){min-width:0;flex:1}</style>
