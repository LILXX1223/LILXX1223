import { test, expect } from '@playwright/test'

test('场景设置保存并在刷新后恢复',async({page})=>{
  await page.goto('/')
  await page.getByPlaceholder('请输入账号').fill(process.env.E2E_ADMIN_USER)
  await page.getByPlaceholder('请输入密码').fill(process.env.E2E_ADMIN_PASSWORD)
  await page.getByRole('button',{name:'登录',exact:true}).click()
  await page.getByRole('link',{name:'系统设置',exact:true}).click()
  await page.getByRole('button',{name:'AI服务配置'}).click()
  await expect(page.getByText('AI业务场景配置')).toBeVisible()
  const row=page.locator('.scene-panel').filter({hasText:'单床病历文字识别'})
  await row.getByRole('spinbutton').first().fill('4096')
  await row.getByRole('button',{name:'保存',exact:true}).click()
  await expect(page.getByText('该场景配置已保存，仅影响后续任务。')).toBeVisible()
  await page.reload()
  await page.getByRole('button',{name:'AI服务配置'}).click()
  await expect(page.locator('.scene-panel').filter({hasText:'单床病历文字识别'}).getByRole('spinbutton').first()).toHaveValue('4096')
  const result=await page.request.get('/api/v1/ai/scenarios/')
  const rows=(await result.json()).data
  expect(rows.find(r=>r.scene==='medical_single').max_tokens).toBe(4096)
  expect(rows.find(r=>r.scene==='medical_batch').max_tokens).toBe(8192)
})

// 批量队列与会话恢复的浏览器测试位于 medical-batch-ui.spec.js。
