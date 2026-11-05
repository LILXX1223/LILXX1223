<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../services/api'

const props=defineProps({admin:Boolean})
const rows=ref([]),loading=ref(false),error=ref(''),busy=ref(''),testResult=ref(null),openScenes=ref(['medical_single'])
const modeName=value=>({disabled:'非思考',low:'Low',high:'High',max:'Max'}[value]||value)
async function load(){loading.value=true;error.value='';try{rows.value=await api('ai/scenarios/')}
  catch(e){error.value=e.message}finally{loading.value=false}}
async function save(row){busy.value=row.scene
  try{const saved=await api('ai/scenarios/','PATCH',{scene:row.scene,model:row.model,thinking:row.thinking,
    max_tokens:row.max_tokens,timeout_seconds:row.timeout_seconds,stream:row.stream,output_format:row.output_format,revision:row.revision})
    Object.assign(row,saved);ElMessage.success('该场景配置已保存，仅影响后续任务。')
  }catch(e){ElMessage.error(e.message)}finally{busy.value=''}}
async function test(row){busy.value=row.scene;testResult.value=null
  try{const result=await api('ai/scenarios/test/','POST',{scene:row.scene},{timeout:630000});testResult.value={...result,label:row.label}}
  catch(e){testResult.value={ok:false,label:row.label,error:e.message}}finally{busy.value=''}}
onMounted(load)
</script>

<template>
  <div class="section-gap" v-loading="loading">
    <el-divider/><h3>AI业务场景配置</h3>
    <p class="small muted">各场景独立保存；模型输出仍需校验和人工确认。测试使用已保存配置与合成数据。</p>
    <el-alert v-if="error" :title="error" type="error" :closable="false"/>
    <el-collapse v-if="rows.length" v-model="openScenes">
      <el-collapse-item v-for="row in rows" :key="row.scene" :name="row.scene" class="scene-panel">
        <template #title><span class="scene-title"><strong>{{row.label}}</strong><span class="muted small">{{modeName(row.thinking)}} · {{row.max_tokens}} Token · {{row.timeout_seconds}} 秒 · 版本 {{row.revision||'默认'}}</span></span></template>
        <el-form label-position="top" :disabled="!props.admin||!!busy">
          <div class="scene-grid">
            <el-form-item label="模型ID"><el-input v-model="row.model"/></el-form-item>
            <el-form-item label="思考模式"><el-select v-model="row.thinking"><el-option label="非思考" value="disabled"/><el-option label="Low" value="low"/><el-option label="High" value="high"/><el-option label="Max" value="max"/></el-select></el-form-item>
            <el-form-item label="输出Token"><el-input-number v-model="row.max_tokens" :min="128" :max="row.provider==='deepseek'&&row.model==='deepseek-flash'?393216:131072" :step="128" controls-position="right"/></el-form-item>
            <el-form-item label="超时（秒）"><el-input-number v-model="row.timeout_seconds" :min="10" :max="600" controls-position="right"/></el-form-item>
            <el-form-item label="输出形式"><template v-if="row.scene==='assistant_chat'"><el-switch v-model="row.stream"/> <span>{{row.stream?'流式文本':'文本'}}</span></template><span v-else>非流式 JSON</span></el-form-item>
          </div>
        </el-form>
        <div v-if="props.admin" class="actions"><el-button size="small" :disabled="!!busy" @click="save(row)">保存</el-button><el-button size="small" :loading="busy===row.scene" :disabled="!!busy" @click="test(row)">测试</el-button></div>
      </el-collapse-item>
    </el-collapse>
    <p class="small muted">结构化业务固定使用非流式 JSON；聊天可切换流式文本。DeepSeek Flash 的输出上限为393216，其他模型的实际限制由服务商校验。</p>
    <el-alert v-if="testResult" :type="testResult.ok?'success':'error'" :closable="false" :title="testResult.label+'：'+(testResult.ok?'真实调用完成':testResult.error)">
      <template v-if="testResult.model">模型 {{testResult.model}} · 模式 {{testResult.thinking}} · {{testResult.max_tokens}} Token · 超时 {{testResult.timeout_seconds}} 秒 · HTTP {{testResult.http_status??'未取得'}} · {{testResult.elapsed_ms==null?'耗时未取得':(testResult.elapsed_ms/1000).toFixed(2)+' 秒'}} · 结束 {{testResult.finish_reason||'未提供'}} · {{testResult.output_format==='json_object'?(testResult.json_valid?'JSON校验通过':'JSON未通过校验'):'文本输出'}} · 错误码 {{testResult.error_code||'无'}} · 用量 {{JSON.stringify(testResult.usage)}} · 请求 {{testResult.request_id||'未生成'}}</template>
    </el-alert>
  </div>
</template>

<style scoped>
.scene-title{display:flex;align-items:center;justify-content:space-between;gap:16px;width:100%;padding-right:8px}
.scene-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:8px 14px}
.scene-grid :deep(.el-input-number),.scene-grid :deep(.el-select){width:100%}
.scene-grid :deep(.el-form-item){margin-bottom:8px}
@media(max-width:760px){.scene-title{align-items:flex-start;flex-direction:column;gap:0}.scene-panel :deep(.el-collapse-item__header){height:auto;min-height:48px;line-height:22px}}
</style>
