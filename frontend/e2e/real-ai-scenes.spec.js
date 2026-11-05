import { test, expect } from '@playwright/test'

test.skip(process.env.ICU_E2E_REAL_AI !== '1','仅 --real-ai 隔离联调时调用真实服务')
test('真实模型验证四种思考参数和流式聊天配置',async({page})=>{
  test.setTimeout(480000)
  await page.goto('/')
  await page.getByPlaceholder('请输入账号').fill(process.env.E2E_ADMIN_USER)
  await page.getByPlaceholder('请输入密码').fill(process.env.E2E_ADMIN_PASSWORD)
  await page.getByRole('button',{name:'登录',exact:true}).click()
  await expect(page.getByRole('link',{name:'系统设置',exact:true})).toBeVisible()
  const csrf=decodeURIComponent((await page.context().cookies()).find(c=>c.name==='csrftoken').value)
  const headers={'X-CSRFToken':csrf}
  const initial=await page.request.get('/api/v1/ai/scenarios/')
  let scene=(await initial.json()).data.find(row=>row.scene==='medical_single')
  for(const thinking of ['disabled','low','high','max']){
    const save=await page.request.patch('/api/v1/ai/scenarios/',{headers,data:{...scene,thinking},timeout:10000})
    expect(save.ok()).toBeTruthy();scene=(await save.json()).data
    const response=await page.request.post('/api/v1/ai/scenarios/test/',{headers,data:{scene:'medical_single'},timeout:200000})
    const body=await response.json()
    expect(response.ok(),body.error?.message).toBeTruthy()
    expect(body.data.thinking).toBe(thinking);expect(body.data.http_status).toBe(200);expect(body.data.json_valid).toBe(true)
  }
  const chat=await page.request.post('/api/v1/ai/scenarios/test/',{headers,data:{scene:'assistant_chat'},timeout:120000})
  const body=await chat.json()
  expect(chat.ok(),body.error?.message).toBeTruthy();expect(body.data.stream).toBe(true);expect(body.data.thinking).toBe('low')
})
