import { test, expect } from '@playwright/test'

test('系统管理员可见未配置状态，真实失败不会伪装成功', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_PASSWORD, '通过 E2E_ADMIN_PASSWORD 提供现有管理员密码后运行；不将密码写入源码。')
  await page.goto('/')
  await page.getByPlaceholder('请输入账号').fill(process.env.E2E_ADMIN_USER || 'ICU2026')
  await page.getByPlaceholder('请输入密码').fill(process.env.E2E_ADMIN_PASSWORD)
  await page.getByRole('button',{name:'登录',exact:true}).click()
  await page.getByRole('link',{name:'系统设置',exact:true}).click()
  await page.getByRole('button',{name:'AI服务配置'}).click()
  const configResponse=await page.request.get('/api/v1/ai/config/')
  expect(configResponse.ok()).toBeTruthy()
  const before=(await configResponse.json()).data
  test.skip(before.has_key, '当前已有密钥，跳过不应改动现有配置的空状态测试。')
  await expect(page.locator('main')).toContainText('AI服务未配置')
  await page.getByRole('button',{name:'测试连接',exact:true}).click()
  await expect(page.locator('.el-message')).toContainText('请先填写 API Key')
  await page.getByLabel('API地址').fill('https://icu-connection-test.invalid/v1')
  const key='sk-synthetic-connection-test'
  await page.getByPlaceholder('请输入 API Key').fill(key)
  const response=page.waitForResponse(r=>r.url().endsWith('/api/v1/ai/test/')&&r.request().method()==='POST')
  await page.getByRole('button',{name:'测试连接',exact:true}).click()
  expect((await response).ok()).toBeFalsy()
  await expect(page.locator('main')).toContainText(/无法解析|不可指向内网/)
  expect(await page.evaluate(()=>JSON.stringify({...localStorage,...sessionStorage}))).not.toContain(key)
  const after=(await (await page.request.get('/api/v1/ai/config/')).json()).data
  expect(after.has_key).toBe(false)
  expect(JSON.stringify(after)).not.toContain(key)
})
