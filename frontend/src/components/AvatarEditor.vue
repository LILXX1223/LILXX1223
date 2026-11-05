<script setup>
import { ref, watch, onBeforeUnmount, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
const props=defineProps({url:String})
const emit=defineEmits(['change'])
const canvas=ref(null),image=ref(null),sourceUrl=ref(''),zoom=ref(1),x=ref(50),y=ref(50),removed=ref(false),rendering=ref(false)
let serial=0
function clearSource(){if(sourceUrl.value)URL.revokeObjectURL(sourceUrl.value);sourceUrl.value='';image.value=null}
function choose(event){
  const file=event.target.files?.[0];event.target.value='';if(!file)return
  if(file.size>5*1024*1024||!['image/jpeg','image/png','image/webp'].includes(file.type))return ElMessage.warning('头像须为不超过5MB的 JPG、PNG 或 WebP 图片。')
  emit('change',{file:null,remove:false,loading:true})
  clearSource();removed.value=false;sourceUrl.value=URL.createObjectURL(file);const loaded=new Image()
  loaded.onload=()=>{if(loaded.src!==sourceUrl.value)return;image.value=loaded;zoom.value=1;x.value=50;y.value=50;render()}
  loaded.onerror=()=>{clearSource();emit('change',{file:null,remove:false,loading:false});ElMessage.error('头像无法读取，请重新选择图片。')};loaded.src=sourceUrl.value
}
async function render(){
  const current=++serial;await nextTick();if(!canvas.value||!image.value)return
  rendering.value=true;const img=image.value,side=Math.min(img.naturalWidth,img.naturalHeight)/zoom.value
  const sx=(img.naturalWidth-side)*x.value/100,sy=(img.naturalHeight-side)*y.value/100
  const ctx=canvas.value.getContext('2d');ctx.clearRect(0,0,256,256);ctx.drawImage(img,sx,sy,side,side,0,0,256,256)
  canvas.value.toBlob(blob=>{if(current!==serial)return;rendering.value=false;if(blob)emit('change',{file:new File([blob],'avatar.png',{type:'image/png'}),remove:false})},'image/png')
}
function remove(){serial++;clearSource();removed.value=true;emit('change',{file:null,remove:true})}
function restore(){serial++;clearSource();removed.value=false;emit('change',{file:null,remove:false})}
watch([zoom,x,y],render)
onBeforeUnmount(()=>{serial++;clearSource()})
</script>
<template><div class="avatar-editor">
  <div class="preview"><canvas v-if="sourceUrl" ref="canvas" width="256" height="256" aria-label="裁剪头像预览"/><img v-else-if="url&&!removed" :src="url" alt="当前头像"/><span v-else>默认头像</span></div>
  <div class="controls"><div class="actions"><label class="upload-label">选择头像<input type="file" accept="image/jpeg,image/png,image/webp" @change="choose"/></label><el-button v-if="url||sourceUrl" size="small" @click="remove">移除头像</el-button><el-button v-if="removed||sourceUrl" size="small" @click="restore">恢复原头像</el-button></div>
  <small class="muted">JPG、PNG、WebP；最多5MB。保存档案后上传。</small>
  <template v-if="sourceUrl"><div class="crop-row"><span>缩放</span><el-slider v-model="zoom" :min="1" :max="4" :step="0.1"/></div><div class="crop-row"><span>水平</span><el-slider v-model="x" :min="0" :max="100"/></div><div class="crop-row"><span>垂直</span><el-slider v-model="y" :min="0" :max="100"/></div><small v-if="rendering" class="muted">正在生成裁剪预览…</small></template>
  </div></div></template>
<style scoped>
.avatar-editor{display:flex;gap:16px;align-items:flex-start;padding:10px 0}.preview{width:96px;height:96px;display:grid;place-items:center;background:#edf6ff;color:#7292b6;border-radius:50%;overflow:hidden;flex-shrink:0;font-size:12px}.preview canvas,.preview img{width:100%;height:100%;object-fit:cover}.controls{flex:1;min-width:0}.upload-label{display:inline-flex;align-items:center;padding:6px 12px;border:1px solid #c5dcf7;border-radius:5px;background:white;color:#1269cf;cursor:pointer;font-size:12px}.upload-label input{display:none}.crop-row{display:flex;gap:12px;align-items:center;height:30px;font-size:12px;color:#6782a1}.crop-row span{width:30px}.crop-row .el-slider{flex:1}
</style>
