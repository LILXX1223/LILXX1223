<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../services/api'
import { useWorkspace } from '../stores/workspace'

const emit=defineEmits(['saved'])
const s=useWorkspace(), start=ref(null), count=ref(0), items=ref([]), input=ref(''), existing=ref([])
const groupRevision=ref(0), configVersion=ref(0), preview=ref(null), payload=ref(null)
const selectedCount=computed(()=>items.value.filter(item=>item.visible).length)
const openCount=computed(()=>items.value.filter(item=>item.enabled).length)
const extraOpenCount=computed(()=>existing.value.filter(b=>b.enabled&&!items.value.some(i=>i.id===b.id)).length)
const restorable=computed(()=>existing.value.filter(b=>!items.value.some(item=>item.id===b.id)))
const keyFor=value=>String(value??'').trim().toUpperCase().replace(/^0+(?=\d)/,'')
function reset(){
  const data=s.data.bed_configuration
  existing.value=data.beds.map(b=>({...b}))
  items.value=existing.value.filter(b=>b.configured).map(b=>({...b}))
  start.value=data.start_number;count.value=items.value.length
  groupRevision.value=s.data.group.revision;configVersion.value=data.version;preview.value=null
}
function rowFor(number){
  const key=keyFor(number),old=existing.value.find(b=>keyFor(b.number)===key)
  return old?{...old,visible:old.configured?old.visible:true}:{id:null,number:key,visible:true,enabled:false,occupied:false,active_assignment:false}
}
function generate(){
  if(!Number.isInteger(start.value)||start.value<1||!Number.isInteger(count.value)||count.value<0||count.value>500||start.value+count.value-1>999999)
    return ElMessage.warning('请填写有效起始床号和0至500的配置数量。')
  const current=new Map(items.value.map(b=>[keyFor(b.number),b]))
  items.value=Array.from({length:count.value},(_,i)=>current.get(String(start.value+i))||rowFor(start.value+i))
}
function add(value=input.value){
  const number=keyFor(value)
  if(!/^[A-Z0-9]+(?:[-_][A-Z0-9]+)*$/.test(number)||number.length>32||(/^\d+$/.test(number)&&(+number<1||+number>999999)))
    return ElMessage.warning('请输入有效床号，例如 105、101A 或 ICU-1。')
  if(items.value.some(b=>keyFor(b.number)===number))return ElMessage.warning('该床号已在候选列表中。')
  if(items.value.length>=500)return ElMessage.warning('每组最多配置500张床位。')
  items.value.push(rowFor(number));count.value=items.value.length;input.value=''
}
function remove(index){items.value.splice(index,1);count.value=items.value.length}
function selectAll(value){items.value.forEach(item=>{item.visible=value})}
async function openPreview(){
  if(items.value.length!==count.value)return ElMessage.warning('数量已修改，请先点击“生成编号”，或通过添加、移出调整候选床位。')
  payload.value={...s.groupPayload(),revision:groupRevision.value,config_version:configVersion.value,start_number:start.value,
    beds:items.value.map(({id,number,visible,enabled})=>({id,number,visible,enabled}))}
  const result=await s.run(()=>api('beds/configure-preview/','POST',payload.value),'')
  if(result)preview.value=result
}
async function save(){
  const result=await s.run(()=>api('beds/configure/','POST',payload.value),'床位配置已保存')
  if(result){reset();emit('saved')}
}
defineExpose({refreshRevision:()=>{groupRevision.value=s.data.group.revision}})
reset()
watch(()=>s.data.group.id,reset)
</script>

<template>
  <div class="bed-config section-gap">
    <div class="card-title"><h3>本组床位设置</h3><el-button type="primary" :disabled="!s.canEdit" :loading="s.busy" @click="openPreview">保存床位设置</el-button></div>
    <el-form label-position="top"><div class="bed-config-form">
      <el-form-item label="起始床位号"><el-input-number v-model="start" :min="1" :max="999999" :disabled="!s.canEdit" placeholder="请填写"/></el-form-item>
      <el-form-item label="本组床位配置数量"><el-input-number v-model="count" :min="0" :max="500" :disabled="!s.canEdit"/></el-form-item>
      <el-form-item label="开放床位数"><el-input :model-value="openCount+extraOpenCount" disabled/></el-form-item>
      <el-button :disabled="!s.canEdit" @click="generate">生成编号</el-button>
    </div></el-form>
    <p class="muted small">已配置 {{items.length}} 床 · 选择显示 {{selectedCount}} 床 · 实际开放 {{openCount+extraOpenCount}} 床。新增床位默认未开放，需逐床确认；移出配置不会删除床位或改变其开放状态。</p>
    <div class="bed-config-actions"><strong>选择显示床位</strong><el-button size="small" :disabled="!s.canEdit" @click="selectAll(true)">全选</el-button><el-button size="small" :disabled="!s.canEdit" @click="selectAll(false)">取消全选</el-button></div>
    <div class="bed-config-list">
      <div v-for="(item,index) in items" :key="item.id||'new-'+item.number" class="bed-config-item">
        <button type="button" class="bed-config-chip" :class="{selected:item.visible}" :disabled="!s.canEdit" :aria-pressed="item.visible" :title="item.number+'床'" @click="item.visible=!item.visible">{{item.number}}床{{item.visible?' ✓':''}}</button>
        <div class="bed-config-status"><el-switch v-model="item.enabled" size="small" :disabled="!s.canEdit" :aria-label="item.number+'床开放状态'"/><span>{{item.enabled?'开放':'未开放'}}</span><el-button link type="danger" :disabled="!s.canEdit" @click="remove(index)">移出</el-button></div>
        <small v-if="item.occupied||item.active_assignment" class="occupied">{{item.occupied?'在床患者':'已有责任安排'}}</small>
      </div>
    </div>
    <div class="bed-config-add"><el-input v-model="input" :disabled="!s.canEdit" placeholder="自定义床号，如 101A" @keyup.enter="add()"/><el-button :disabled="!s.canEdit" @click="add()">添加床号</el-button><el-select v-if="restorable.length" placeholder="选择已有床位" :disabled="!s.canEdit" @change="add"><el-option v-for="bed in restorable" :key="bed.id" :label="bed.number+'床'" :value="String(bed.number)"/></el-select></div>
    <p class="muted small">点击床号只改变显示；修改生成范围不会重编号原床位。存在在床患者或当前、未来责任安排的床位不可隐藏、关闭或移出。</p>
    <el-dialog :model-value="!!preview" title="确认床位配置影响" width="580px" :close-on-click-modal="false" @close="preview=null">
      <template v-if="preview"><p>本次配置 {{preview.configured_count}} 床；新增 {{preview.added.length}} 床，移出 {{preview.removed.length}} 床，状态变化 {{preview.changed.length}} 床。</p>
        <p v-if="preview.added.length" class="small">新增：{{preview.added.join('、')}}</p><p v-if="preview.removed.length" class="small">移出：{{preview.removed.join('、')}}（保留原床位及历史记录）</p><p v-if="preview.changed.length" class="small">状态变化：{{preview.changed.join('、')}}</p>
        <el-alert v-if="preview.unsafe.length" type="error" :closable="false" title="以下床位有在床患者或当前护理责任，不能隐藏、关闭或移出。"/><p v-for="item in preview.unsafe" :key="item.number" class="small">{{item.number}}床：{{item.reason}}</p>
      </template><template #footer><el-button @click="preview=null">取消</el-button><el-button type="primary" :disabled="!preview?.can_save" :loading="s.busy" @click="save">确认保存</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
.bed-config-form{display:flex;gap:20px;align-items:flex-end;flex-wrap:wrap}.bed-config-form .el-form-item{margin-bottom:8px}.bed-config-form .el-form-item:nth-child(3){width:150px}.bed-config-form>.el-button{margin-bottom:8px}.bed-config-actions{display:flex;align-items:center;gap:8px;margin:16px 0 10px}.bed-config-list{display:flex;flex-wrap:wrap;gap:10px}.bed-config-item{width:112px;border:1px solid #e4edf7;border-radius:6px;padding:5px;text-align:center}.bed-config-chip{width:100%;padding:6px 3px;border:1px solid #e3e8ed;border-radius:4px;background:#f3f5f7;color:#536477;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.bed-config-chip.selected{background:#e9f3ff;border-color:#a8ceff;color:#176bce}.bed-config-chip:disabled{cursor:default}.bed-config-status{display:flex;align-items:center;justify-content:center;gap:4px;font-size:11px;color:#66788b}.bed-config-status .el-button{font-size:11px}.occupied{color:#d48816}.bed-config-add{display:flex;gap:8px;align-items:center;margin-top:14px;flex-wrap:wrap}.bed-config-add .el-input,.bed-config-add .el-select{width:200px}
</style>
