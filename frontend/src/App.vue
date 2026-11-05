<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { HeartPulse, House, Users, CalendarDays, BedDouble, ChartColumn, Settings, Bell, LogOut, UserRound } from 'lucide-vue-next'
import { ElMessage } from 'element-plus'
import { useWorkspace } from './stores/workspace'
import { api } from './services/api'
import RealtimeClock from './components/RealtimeClock.vue'
import LoginAtmosphere from './components/LoginAtmosphere.vue'
const store=useWorkspace(), ready=ref(false), loginBusy=ref(false), notifications=ref(false), loginError=ref('')
const credentials=ref({username:'',password:''})
const menus=[['/overview','总览',House],['/personnel','人员信息',Users],['/scheduling','排班管理',CalendarDays],['/beds','床位分配',BedDouble],['/workload','工作量统计',ChartColumn],['/settings','系统设置',Settings]]
let timer
async function signIn(){loginBusy.value=true;loginError.value='';try{store.user=await api('auth/login/','POST',credentials.value);await store.load();credentials.value.password=''}catch(e){loginError.value=e.message}finally{loginBusy.value=false}}
async function signOut(){try{await api('auth/logout/','POST',{});store.user=null;store.data=null;store.group=''}catch(e){ElMessage.error(e.message)}}
async function reload(){try{await store.load()}catch(e){ElMessage.error(e.message)}}
onMounted(async()=>{try{store.user=await api('auth/me/');await store.load()}catch{}finally{ready.value=true}timer=setInterval(()=>{if(store.user&&!store.busy&&!document.hidden)store.load(true).catch(()=>{})},15000)})
onUnmounted(()=>clearInterval(timer))
</script>
<template>
  <div v-if="!ready" class="boot">正在连接 ICU 工作台…</div>
  <div v-else-if="!store.user || !store.data" class="login-page">
    <LoginAtmosphere />
    <div class="login-brand"><HeartPulse :size="52"/><h1>ICU 护士排班管理系统</h1><p>让每一班的安排，都有清晰的依据。</p><div class="login-pills"><span>人员与排班</span><span>床位责任分配</span><span>工作量联动</span></div></div>
    <div class="login-card"><h2>登录工作台</h2><p class="muted">使用已授权的病区管理账号</p><el-form @submit.prevent="signIn" label-position="top"><el-form-item label="账号"><el-input v-model="credentials.username" autocomplete="username" placeholder="请输入账号"/></el-form-item><el-form-item label="密码"><el-input v-model="credentials.password" type="password" show-password autocomplete="current-password" placeholder="请输入密码"/></el-form-item><el-alert v-if="loginError" :title="loginError" type="error" :closable="false"/><el-button type="primary" native-type="submit" :loading="loginBusy" class="full">登录</el-button></el-form><p class="muted small">当前为合成资料研究原型。管理演示账号的信息见项目 README。</p></div>
  </div>
  <div v-else class="app-shell">
    <aside class="sidebar"><div class="brand"><HeartPulse :size="34"/><div>ICU 护士排班<span>管理系统</span></div></div><nav><router-link v-for="[path,label,icon] in menus" :key="path" :to="path" :class="{active:$route.path.startsWith(path)}"><component :is="icon" :size="21"/><span>{{label}}</span></router-link></nav><div class="sidebar-bottom"><HeartPulse :size="46"/><p>护理协作 · 安全有序</p><span>ICU MANAGEMENT</span></div></aside>
    <div class="workspace"><header class="topbar"><span class="crumb">{{store.data.group.rules.ward_name}} <span class="muted"> / 护理工作台</span></span><div class="top-actions"><RealtimeClock :server-time="store.data.server_time"/><el-select v-if="store.data.groups.length>1" v-model="store.group" @change="reload" style="width:115px"><el-option v-for="g in store.data.groups" :key="g.id" :value="String(g.id)" :label="g.name"/></el-select><button class="icon-button" aria-label="通知" @click="notifications=true"><el-badge :value="store.data.notifications.filter(n=>!n.read).length" :hidden="!store.data.notifications.some(n=>!n.read)"><Bell :size="21"/></el-badge></button><span class="user-avatar"><UserRound :size="22"/></span><b>{{store.user.label}}</b><button class="icon-button" aria-label="退出登录" @click="signOut"><LogOut :size="18"/></button></div></header>
    <div class="demo-strip"><span class="status-dot"></span>{{store.data.synthetic_demo?'合成数据演示环境':'当前包含非合成患者资料'}} <span>·</span> {{store.data.ai.configured?'AI 服务已保存配置':'AI服务未配置'}} <span>·</span> 统计来源：正式发布记录 <span v-if="!store.canEdit">· 当前角色只读</span><button @click="reload">刷新数据</button></div>
    <main v-loading="store.loading"><router-view/></main><footer class="footer">ICU 护士排班管理系统 · 所有推荐均须负责人核对确认</footer></div>
    <el-drawer v-model="notifications" title="消息通知" size="440px"><el-button @click="store.run(()=>api('notifications/read/','POST',{}),'已标为已读')">全部标为已读</el-button><el-empty v-if="!store.data.notifications.length" description="暂无通知"/><div v-for="n in store.data.notifications" :key="n.id" class="notice"><el-tag v-if="!n.read" size="small">未读</el-tag><p>{{n.message}}</p><small class="muted">{{new Date(n.created_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})}}</small></div></el-drawer>
  </div>
</template>
