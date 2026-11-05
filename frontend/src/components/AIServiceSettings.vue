<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useWorkspace } from '../stores/workspace'
import { api } from '../services/api'
import AISceneSettings from './AISceneSettings.vue'

const s=useWorkspace()
const admin=computed(()=>s.user.role==='system_admin')
const defaults={provider:'deepseek',base_url:'https://api.deepseek.com',model:'deepseek-flash',revision:0}
const form=ref({...defaults}), saved=ref(null), newKey=ref(''), changingKey=ref(false)
const loading=ref(false), saving=ref(false), testing=ref(false), loadError=ref(''), testResult=ref(null)
let disposed=false

function apply(value){
  saved.value=value
  form.value={provider:value.provider||defaults.provider,base_url:value.base_url||defaults.base_url,
    model:value.model||'',revision:value.revision}
  newKey.value='';changingKey.value=!value.has_key;testResult.value=null
}
async function load(){
  loading.value=true;loadError.value=''
  try{const result=await api('ai/config/');if(!disposed)apply(result)}
  catch(error){if(!disposed)loadError.value=error.message}
  finally{if(!disposed)loading.value=false}
}
function changeProvider(value){
  if(value==='deepseek'){
    form.value.base_url='https://api.deepseek.com'
    form.value.model='deepseek-flash'
  }else if(value==='openai_compatible'){
    form.value.base_url=''
    form.value.model=''
  }
}
function payload(){
  const data={provider:form.value.provider,base_url:form.value.base_url.trim(),model:form.value.model.trim()}
  if(changingKey.value && newKey.value) data.api_key=newKey.value
  return data
}
function valid(){
  if(!form.value.base_url.trim()||!form.value.model.trim()){
    ElMessage.warning('请填写 API 地址和模型名称。');return false
  }
  if(changingKey.value && saved.value?.has_key && !newKey.value){
    ElMessage.warning('请填写新密钥，或取消更换以保留原密钥。');return false
  }
  return true
}
async function testConnection(){
  if(!admin.value||!valid())return
  if(!newKey.value&&!saved.value?.has_key){ElMessage.warning('请先填写 API Key，再测试连接。');return}
  testing.value=true;testResult.value=null
  try{const result=await api('ai/test/','POST',payload());if(!disposed)testResult.value={ok:result.ok===true,message:result.message||'连接测试完成。'}}
  catch(error){if(!disposed)testResult.value={ok:false,message:error.message}}
  finally{if(!disposed)testing.value=false}
}
async function saveConfig(){
  if(!admin.value||!valid())return
  saving.value=true
  try{
    const result=await api('ai/config/','PATCH',{...payload(),revision:form.value.revision})
    if(disposed)return
    apply(result);ElMessage.success('配置已保存。连接是否可用请单独测试。')
    try{await s.load(true)}catch{}
  }catch(error){if(!disposed)ElMessage.error(error.message)}
  finally{if(!disposed)saving.value=false}
}
function cancelKey(){changingKey.value=false;newKey.value=''}
watch([()=>form.value.provider,()=>form.value.base_url,()=>form.value.model,newKey],()=>{testResult.value=null})
onMounted(load)
onBeforeUnmount(()=>{disposed=true;newKey.value=''})
</script>

<template>
  <section class="card" v-loading="loading">
    <div class="card-title"><div><h2>AI服务配置</h2><p class="muted small">统一配置大模型服务，业务功能分别进行结果校验和人工核对。</p></div></div>
    <el-alert v-if="loadError" :title="'无法读取 AI 服务配置：'+loadError" type="error" :closable="false"/>
    <template v-else-if="saved">
      <el-alert :title="saved.configured?'已保存 AI 服务配置；连接状态需通过测试确认。':'AI服务未配置：请填写 API 地址、模型名称和 API Key。'" :type="saved.configured?'info':'warning'" :closable="false"/>
      <el-form label-position="top" style="max-width:760px" :disabled="!admin || saving || testing">
        <div class="form-grid">
          <el-form-item label="AI服务商">
            <el-select v-model="form.provider" class="ai-full-width" @change="changeProvider">
              <el-option label="DeepSeek" value="deepseek"/>
              <el-option label="其他 OpenAI 兼容服务" value="openai_compatible"/>
            </el-select>
          </el-form-item>
          <el-form-item label="模型名称">
            <el-input v-model="form.model" placeholder="例如 deepseek-flash" maxlength="200"/>
          </el-form-item>
        </div>
        <el-form-item label="API地址">
          <el-input v-model="form.base_url" placeholder="请输入 HTTPS API 地址" maxlength="500"/>
        </el-form-item>
        <el-form-item label="API Key">
          <div class="ai-key-input">
            <el-input v-if="admin && changingKey" v-model="newKey" type="password" show-password autocomplete="new-password" placeholder="请输入 API Key" maxlength="1000"/>
            <span v-else class="muted small">{{saved.has_key?'已保存，出于安全原因不显示':'尚未设置'}}</span>
            <el-button v-if="admin && saved.has_key && !changingKey" @click="changingKey=true">更换密钥</el-button>
            <el-button v-if="admin && saved.has_key && changingKey" @click="cancelKey">取消更换</el-button>
          </div>
        </el-form-item>
      </el-form>
      <el-alert v-if="testResult" :title="testResult.message" :type="testResult.ok?'success':'error'" :closable="false"/>
      <div v-if="admin" class="actions section-gap">
        <el-button :loading="testing" :disabled="loading || saving" @click="testConnection">测试连接</el-button>
        <el-button type="primary" :loading="saving" :disabled="loading || testing" @click="saveConfig">保存配置</el-button>
      </div>
      <div class="role-info section-gap">
        <p v-if="admin">输入的密钥只发送给后端保存，已保存密钥不会返回页面。测试连接使用当前表单内容，不会自动保存。</p>
        <p v-else>当前账号可查看服务配置；修改和测试由系统管理员负责。</p>
        <p class="small">配置保存不代表已通过连接测试。涉及真实患者资料时仍须满足医院授权与数据安全要求；提取结果须由人员核对。</p>
      </div>
      <AISceneSettings :admin="admin"/>
    </template>
    <el-button v-if="loadError" @click="load">重新加载</el-button>
  </section>
</template>

<style scoped>
.ai-full-width{width:100%}.ai-key-input{display:flex;align-items:center;gap:10px;width:100%}.ai-key-input .el-input{max-width:510px}.card-title p{margin:8px 0 0}
</style>
