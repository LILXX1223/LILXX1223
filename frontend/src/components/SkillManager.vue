<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../services/api'
import { useWorkspace } from '../stores/workspace'

const props=defineProps({modelValue:Boolean})
const emit=defineEmits(['update:modelValue','changed'])
const s=useWorkspace(), items=ref([]), total=ref(0), query=ref(''), status=ref(''), selected=ref(null), editing=ref(false), form=ref({}), loading=ref(false), saving=ref(false)
const filtered=computed(()=>items.value.filter(x=>(!query.value||[x.name,x.code,x.category,x.description].some(v=>v?.toLowerCase().includes(query.value.trim().toLowerCase())))&&(!status.value||(status.value==='enabled')===x.enabled)))
const enabled=computed(()=>items.value.filter(x=>x.enabled).length)
async function load(){
  const group=s.group;loading.value=true
  try{const data=await api('skills/?'+new URLSearchParams({group}));if(s.group!==group)return;items.value=data.items;total.value=data.active_nurses
    if(selected.value){selected.value=items.value.find(x=>x.id===selected.value.id)||null;if(!editing.value&&selected.value)form.value={...selected.value}}
  }catch(e){ElMessage.error(e.message)}finally{loading.value=false}
}
function choose(row){selected.value=row;editing.value=false;form.value={...row}}
function add(){selected.value=null;form.value={code:'',name:'',category:'',description:'',enabled:true,participates_bed:true,participates_group:true};editing.value=true}
function edit(){if(selected.value){form.value={...selected.value};editing.value=true}}
function cancel(){if(selected.value)form.value={...selected.value};editing.value=false}
async function save(){
  if(!form.value.code?.trim()||!form.value.name?.trim())return ElMessage.warning('请填写技能名称和技能代码。')
  const group=Number(s.group), id=selected.value?.id, fields=['code','name','category','description','enabled','participates_bed','participates_group']
  saving.value=true
  try{const result=await api(id?`skills/${id}/`:'skills/',id?'PATCH':'POST',{group,...Object.fromEntries(fields.map(k=>[k,form.value[k]])),...(id?{revision:selected.value.revision}:{})})
    selected.value=result;editing.value=false;ElMessage.success('技能项目已保存');await load();emit('changed')
  }catch(e){ElMessage.error(e.message)}finally{saving.value=false}
}
async function remove(row){
  if(saving.value)return
  try{await ElMessageBox.confirm(`确认删除“${row.name}”（${row.code}）？当前护士关联将移除，已发布的历史记录保留。`,'确认删除技能',{type:'warning',confirmButtonText:'确认删除',cancelButtonText:'取消'})}catch{return}
  saving.value=true
  try{await api(`skills/${row.id}/`,'DELETE',{group:Number(s.group),revision:row.revision});if(selected.value?.id===row.id){selected.value=null;editing.value=false}ElMessage.success('技能项目已删除');await load();emit('changed')}
  catch(e){ElMessage.error(e.message)}finally{saving.value=false}
}
async function toggle(row){
  if(saving.value)return
  saving.value=true
  try{await api(`skills/${row.id}/`,'PATCH',{group:Number(s.group),revision:row.revision,enabled:!row.enabled});ElMessage.success(row.enabled?'技能已停用':'技能已启用');await load();emit('changed')}
  catch(e){ElMessage.error(e.message)}finally{saving.value=false}
}
watch(()=>props.modelValue,open=>{if(open){query.value='';status.value='';selected.value=null;editing.value=false;load()}})
watch(()=>s.group,()=>{emit('update:modelValue',false);items.value=[];selected.value=null})
</script>
<template>
<el-dialog :model-value="modelValue" title="专科技能项目管理" width="min(1160px, 94vw)" class="skill-manager-dialog" :close-on-click-modal="false" @close="emit('update:modelValue',false)">
  <p class="muted small">管理 {{s.data.group.name}} 的技能项目与护士关联。覆盖率以本组在岗护士为分母，停用技能不参与新的有效匹配。</p>
  <div class="skill-summary"><div><label>技能项目</label><strong>{{items.length}}</strong><small>项</small></div><div><label>已启用</label><strong>{{enabled}}</strong><small>项</small></div><div><label>已停用</label><strong>{{items.length-enabled}}</strong><small>项</small></div><div><label>有效在岗护士</label><strong>{{total}}</strong><small>人</small></div></div>
  <div class="skill-layout" v-loading="loading"><section class="skill-list"><div class="card-title"><h3>技能项目列表</h3><el-button v-if="s.canEdit" type="primary" size="small" @click="add">＋ 新增技能</el-button></div>
    <div class="filters"><el-input v-model="query" placeholder="搜索名称、代码或分类" clearable/><el-select v-model="status" placeholder="全部状态" clearable><el-option label="启用" value="enabled"/><el-option label="停用" value="disabled"/></el-select></div>
    <el-table :data="filtered" row-key="id" highlight-current-row max-height="450" @row-click="choose"><el-table-column prop="name" label="技能名称" min-width="100"/><el-table-column prop="category" label="分类" width="80"/><el-table-column prop="description" label="说明" min-width="130" show-overflow-tooltip/>
      <el-table-column label="关联护士" width="90"><template #default="{row}"><el-popover trigger="click" :width="260"><template #reference><el-button link type="primary" @click.stop>{{row.nurse_count}} 人</el-button></template><b>{{row.name}} · 关联护士</b><p v-if="!row.nurses.length" class="muted small">暂无关联护士</p><div v-for="n in row.nurses" :key="n.id" class="small skill-person">{{n.name}} · {{n.employee_id}} · {{({active:'在岗',paused:'停排',left:'离职'})[n.status]}}</div></el-popover></template></el-table-column>
      <el-table-column label="覆盖率" width="85"><template #default="{row}">{{Number(row.coverage).toFixed(1)}}%</template></el-table-column><el-table-column label="状态" width="76"><template #default="{row}"><el-tag :type="row.enabled?'success':'info'" size="small">{{row.enabled?'启用':'停用'}}</el-tag></template></el-table-column>
      <el-table-column v-if="s.canEdit" label="操作" width="160" fixed="right"><template #default="{row}"><el-button link type="primary" :disabled="saving" @click.stop="choose(row);edit()">编辑</el-button><el-button link :disabled="saving" :type="row.enabled?'warning':'success'" @click.stop="toggle(row)">{{row.enabled?'停用':'启用'}}</el-button><el-button link type="danger" :disabled="saving" @click.stop="remove(row)">删除</el-button></template></el-table-column></el-table>
    <p class="muted small">显示 {{filtered.length}} / {{items.length}} 项；点击关联人数查看名单。</p>
  </section><aside class="skill-details"><div class="card-title"><h3>{{editing?(selected?'编辑技能':'新增技能'):'技能详情'}}</h3><el-button v-if="selected&&!editing&&s.canEdit" link type="primary" @click="edit">编辑</el-button></div>
    <el-empty v-if="!selected&&!editing" description="选择左侧技能查看详情" :image-size="68"/>
    <el-form v-else label-position="top" :disabled="!editing"><el-form-item label="技能名称 *"><el-input v-model="form.name" maxlength="60"/></el-form-item><el-form-item label="技能代码 *"><el-input v-model="form.code" maxlength="40" :disabled="!!selected"/><small class="muted">代码为稳定标识，创建后不可修改。</small></el-form-item><el-form-item label="技能分类"><el-input v-model="form.category" maxlength="60"/></el-form-item><el-form-item label="技能说明"><el-input v-model="form.description" type="textarea" :rows="3" maxlength="500"/></el-form-item><el-checkbox v-model="form.enabled">启用技能项目</el-checkbox><br/><el-checkbox v-model="form.participates_bed">参与智能床位匹配</el-checkbox><br/><el-checkbox v-model="form.participates_group">作为护士能力配置参考</el-checkbox><p v-if="selected" class="muted small section-gap">当前关联 {{selected.nurse_count}} 人；在岗有效覆盖 {{Number(selected.coverage).toFixed(1)}}%。</p></el-form>
    <div v-if="editing" class="window-footer"><el-button @click="cancel">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存技能</el-button></div>
  </aside></div>
  <template #footer><el-button @click="emit('update:modelValue',false)">关闭</el-button></template>
</el-dialog>
</template>
<style scoped>
.skill-summary{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:16px 0}.skill-summary>div{background:#f3f8ff;border:1px solid #dfebf8;border-radius:7px;padding:12px 16px}.skill-summary label{display:block;color:#768ca5;font-size:12px}.skill-summary strong{display:inline-block;color:#076dec;font-size:24px;margin:5px 5px 0 0}.skill-summary small{color:#768ca5}.skill-layout{display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:16px}.skill-list,.skill-details{border:1px solid #e1ebf6;border-radius:8px;padding:15px;min-width:0}.skill-list .filters .el-input{width:220px}.skill-list .filters .el-select{width:130px}.skill-details .el-form-item{margin-bottom:12px}.skill-person{padding:6px 0;border-bottom:1px solid #edf3fa}.skill-manager-dialog :deep(.el-dialog__body){max-height:78vh}
</style>
