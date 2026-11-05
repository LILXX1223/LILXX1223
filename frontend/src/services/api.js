// Local development keeps using the Vite /api proxy.  A hosted build may
// provide VITE_API_BASE_URL (or a runtime window value) for a real HTTPS
// Django endpoint; it must never silently point at a developer's localhost.
// The hosted Site is static, so it cannot read the local Vite proxy. Keep the
// current public HTTPS tunnel as a deployment fallback; local development can
// still override it with VITE_API_BASE_URL or window.__ICU_API_BASE__.
const hostedApiFallback = 'https://scenes-nikon-sequences-dui.trycloudflare.com'
const configuredApiBase = (typeof window !== 'undefined' && window.__ICU_API_BASE__) || import.meta.env.VITE_API_BASE_URL || ((typeof window !== 'undefined' && window.location.hostname.endsWith('.chatgpt.site')) ? hostedApiFallback : '')
const apiBase = String(configuredApiBase).replace(/\/$/, '')
const hostedWithoutBackend = typeof window !== 'undefined' && window.location.hostname.endsWith('.chatgpt.site') && !apiBase
const endpoint = path => `${apiBase}/api/v1/${path}`
let csrfToken = ''
let sessionToken = ''

function hostedBackendError() {
  const error = new Error('当前发布站点尚未配置可访问的 Django 后端地址，暂不能登录。请先配置 HTTPS API 地址后再重试。')
  error.code = 'backend_not_configured'
  error.retryable = false
  return error
}

export async function api(path, method = 'GET', data, options = {}) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), options.timeout || 40000)
  try {
    if (method !== 'GET' && !csrfToken) await ensureCsrfCookie()
    const csrf = csrfToken || document.cookie.split('; ').find(v => v.startsWith('csrftoken='))?.split('=')[1] || ''
    const response = await fetch(endpoint(path), { method, credentials: 'include', signal: controller.signal,
      headers: { 'Content-Type': 'application/json', ...(sessionToken ? { 'Authorization': `Bearer ${sessionToken}` } : {}), ...(method !== 'GET' ? { 'X-CSRFToken': decodeURIComponent(csrf) } : {}) },
      ...(data !== undefined ? { body: JSON.stringify(data) } : {}) })
    let payload
    try { payload = await response.json() } catch {
      if (hostedWithoutBackend) throw hostedBackendError()
      const error = new Error(response.status===504?'代理等待后端响应超时，请稍后查询任务状态。':`服务返回无效数据（HTTP ${response.status}），请检查后端。`)
      error.status=response.status;error.code=response.status===504?'gateway_timeout':'invalid_http_response';throw error
    }
    if (!response.ok) {
      let message=payload.error?.message || payload.detail || '操作失败，请刷新重试。'
      if(response.status===401||payload.error?.code==='not_authenticated'||((response.status===403)&&/Authentication credentials|not authenticated|身份认证信息未提供/i.test(String(message))))message='Django登录已失效，请重新登录后重试。'
      if(payload.error?.request_id&&!String(message).includes(payload.error.request_id))message+=`（请求编号 ${payload.error.request_id}）`
      const error = new Error(message)
      error.issues = payload.error?.issues || []; error.status = response.status
      error.code=payload.error?.code || ({504:'gateway_timeout',429:'rate_limit',401:'not_authenticated'}[response.status])
      if(response.status===504&&!payload.error?.message)error.message='代理等待后端响应超时，请查询任务状态。'
      error.requestId=payload.error?.request_id; error.retryable=payload.error?.retryable; throw error
    }
    // Django rotates its CSRF secret during login.  The token obtained for
    // the login request is no longer valid for later state-changing calls.
    // Fetch the new token lazily before the next write; never bypass CSRF.
    if (path === 'auth/login/') {
      sessionToken = payload.data?.session_token || ''
      csrfToken = ''
    }
    if (path === 'auth/logout/') { sessionToken = ''; csrfToken = '' }
    return payload.data
  } catch (error) {
    if (error.name === 'AbortError') { const timeout=new Error('浏览器等待响应超时，后端任务可能仍在执行，请查询任务状态。');timeout.code='frontend_timeout';throw timeout }
    if (error instanceof TypeError) { const network=new Error('无法连接后端服务，请检查网络或服务状态。');network.code='network';throw network }
    throw error
  } finally { clearTimeout(timer) }
}
async function ensureCsrfCookie() {
  if (hostedWithoutBackend) throw hostedBackendError()
  const response = await fetch(endpoint('auth/csrf/'), { credentials: 'include' })
  if (!response.ok) return
  // Most browsers persist the Set-Cookie header.  When a local proxy or a
  // hosted preview does not, the endpoint also returns the token so we can
  // seed the same-site cookie explicitly; Django still validates both cookie
  // and X-CSRFToken on the state-changing request.
  try {
    const payload = await response.json()
    const token = payload?.data?.csrf_token
    if (token) {
      // Keep the masked token in memory as well as seeding the local cookie.
      // A cross-origin hosted Site cannot read the API-domain cookie, while
      // the Django endpoint can still validate this token through the
      // allow-listed CORS middleware.
      csrfToken = token
      if (!document.cookie.includes('csrftoken=')) document.cookie = `csrftoken=${encodeURIComponent(token)}; Path=/; SameSite=Lax`
    }
  } catch { /* the cookie may already have been set */ }
}
export async function uploadFile(path, form) {
  if (!csrfToken) await ensureCsrfCookie()
  const csrf=csrfToken || document.cookie.split('; ').find(v=>v.startsWith('csrftoken='))?.split('=')[1]||''
  if (hostedWithoutBackend) throw hostedBackendError()
  const response=await fetch(endpoint(path),{method:'POST',credentials:'include',headers:{'X-CSRFToken':decodeURIComponent(csrf),...(sessionToken ? {'Authorization':`Bearer ${sessionToken}`} : {})},body:form})
  let payload;try{payload=await response.json()}catch{throw hostedWithoutBackend?hostedBackendError():new Error('服务未返回有效数据，请检查后端。')}
  if(!response.ok)throw new Error(payload.error?.message||'上传失败，请刷新后重试。')
  return payload.data
}
export async function streamApi(path, data, onDelta) {
  const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),630000)
  try{
    if(!csrfToken)await ensureCsrfCookie()
    const csrf=csrfToken || document.cookie.split('; ').find(v=>v.startsWith('csrftoken='))?.split('=')[1]||''
    const response=await fetch(endpoint(path),{method:'POST',credentials:'include',signal:controller.signal,
      headers:{'Content-Type':'application/json','X-CSRFToken':decodeURIComponent(csrf),...(sessionToken ? {'Authorization':`Bearer ${sessionToken}`} : {})},body:JSON.stringify(data)})
    if(!response.ok){let payload={};try{payload=await response.json()}catch{}throw new Error(payload.error?.message||payload.detail||'请求失败。')}
    if(!(response.headers.get('content-type')||'').includes('text/event-stream'))return (await response.json()).data
    const reader=response.body.getReader(), decoder=new TextDecoder();let buffer='',result=null
    while(true){const {value,done}=await reader.read();buffer+=decoder.decode(value||new Uint8Array(),{stream:!done})
      let pos;while((pos=buffer.indexOf('\n\n'))>=0){const block=buffer.slice(0,pos);buffer=buffer.slice(pos+2)
        const line=block.split('\n').find(x=>x.startsWith('data: '));if(!line)continue
        const event=JSON.parse(line.slice(6));if(event.type==='delta')onDelta(event.text)
        if(event.type==='error')throw new Error(event.message)
        if(event.type==='done')result=event.data
      }
      if(done)break
    }
    if(!result)throw new Error('AI流式响应未完成；请核对历史会话后重试。')
    return result
  }catch(error){if(error.name==='AbortError')throw new Error('AI请求超时，请核对历史会话后重试。');throw error}
  finally{clearTimeout(timer)}
}
export const today = () => new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date())
export const addDays = (value, days) => { const d = new Date(value + 'T12:00:00'); d.setDate(d.getDate() + days); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}` }
export const monday = (value) => addDays(value, -(new Date(value + 'T12:00:00').getDay() + 6) % 7)
export const shiftName = code => ({ A: 'A班', DAY: '白班', N: 'N班', POST: '夜下休', OFF: '休', TRAIN: '培训' }[code] || code || '—')
export const statusName = code => ({ pending: '待审批', approved: '已批准', rejected: '已驳回', withdrawn: '已撤销', superseded: '已由新修订替代', applied: '已应用至草稿', published: '已应用并发布', invalidated: '人员已删除，保留历史' }[code] || code)
export function downloadCSV(name, rows) {
  const safe = v => { let s=String(v??''); if (/^[=+@-]/.test(s)) s="'"+s; return '"'+s.replaceAll('"','""')+'"' }
  const url=URL.createObjectURL(new Blob(['\ufeff'+rows.map(row=>row.map(safe).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8;'}))
  const a=document.createElement('a'); a.href=url; a.download=name; a.click(); URL.revokeObjectURL(url)
}
