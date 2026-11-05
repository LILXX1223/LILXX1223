import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { api, today, monday } from '../services/api'
export const useWorkspace = defineStore('workspace', () => {
  const user=ref(null), data=ref(null), loading=ref(false), date=ref(today()), week=ref(monday(today())), shift=ref('A'), group=ref(''), busy=ref(false)
  let latest=0
  const canEdit=computed(()=>['group_manager','system_admin'].includes(user.value?.role))
  async function load(silent=false) {
    const seq=++latest; if (!silent) loading.value=true
    try {
      const value=await api('workspace/?'+new URLSearchParams({ date:date.value, week:week.value, shift:shift.value, ...(group.value?{group:group.value}:{}) }))
      if(seq===latest){data.value=value; user.value=value.user; group.value=String(value.group.id)}
    } finally { if(seq===latest) loading.value=false }
  }
  async function run(fn, message='操作成功') {
    if(busy.value) return null
    busy.value=true
    try { const result=await fn(); if(message) ElMessage.success(message); await load(true); return result }
    catch(error){ ElMessage.error(error.message + (error.issues?.length ? ' '+error.issues.map(x=>x.message).slice(0,3).join('；') : '')); return null }
    finally { busy.value=false }
  }
  const groupPayload=()=>({group:Number(group.value)})
  const bedPayload=()=>({ ...groupPayload(), id:data.value.board.id, date:date.value, shift:shift.value, revision:data.value.board.revision, fingerprint:data.value.board.fingerprint })
  return {user,data,loading,date,week,shift,group,busy,canEdit,load,run,groupPayload,bedPayload}
})
