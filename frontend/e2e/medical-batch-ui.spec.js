import { test, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'

test.skip(process.env.ICU_AI_ALLOW_MOCK !== 'true','仅 --mock-ai 隔离浏览器测试执行本地模拟识别')
test('模拟识别明确标注，失败后单床重试、刷新恢复并人工导入',async({page})=>{
  expect(process.env.ICU_E2E_ISOLATED).toBe('1')
  expect(process.env.ICU_AI_ALLOW_MOCK).toBe('true')
  const shell=code=>execFileSync(resolve('..','.venv/bin/python'),['manage.py','shell','-c',
    "import os; assert os.environ.get('ICU_E2E_ISOLATED')=='1'; "+code],{cwd:resolve('..'),env:process.env})
  shell("from scheduling.models import CareGroup,Bed; g=CareGroup.objects.get(name='第3组'); [Bed.objects.get_or_create(group=g,number=str(n)) for n in range(36,39)]")
  await page.route('**/api/v1/workspace/**',async route=>{const response=await route.fetch(),body=await response.json();body.data.ai.configured=true;await route.fulfill({response,json:body})})
  let runs=0
  await page.route('**/api/v1/medical-batches/*/run/',async route=>{
    const response=await route.fetch();runs++
    if(runs===1)shell("from scheduling.models import CareGroup,Bed,Patient; g=CareGroup.objects.get(name='第3组'); b=Bed.objects.get(group=g,number='37'); Patient.objects.create(group=g,bed=b,name='临时合成占床',code='TEMP-37',synthetic=True)")
    await route.fulfill({response})
  })
  await page.goto('/')
  await page.getByPlaceholder('请输入账号').fill('group3');await page.getByPlaceholder('请输入密码').fill(process.env.E2E_PASSWORD)
  await page.getByRole('button',{name:'登录',exact:true}).click();await page.getByRole('link',{name:'床位分配',exact:true}).click()
  await page.getByText('批量识别',{exact:true}).click()
  await page.getByText('同时为病历中明确指定的空床导入合成病例',{exact:true}).click()
  const input=page.locator('.ai-box textarea')
  const text=[36,37,38].map(n=>`${n}床：姓名：合成${n}；诊断：肺炎；当前机械通气。`).join('\n')
  await input.fill(text);await page.locator('.ai-box').getByRole('button',{name:'AI识别病历',exact:true}).click()
  const progress=page.locator('.batch-progress')
  await expect(progress).toContainText('成功 2');await expect(progress).toContainText('失败 1')
  await expect(input).toHaveValue(text)
  shell("from scheduling.models import Patient; Patient.objects.filter(code='TEMP-37',synthetic=True).delete()")
  await progress.locator('.batch-item').filter({hasText:'37床'}).getByRole('button',{name:'重试此床'}).click()
  await expect(progress).toContainText('成功 3')
  await expect(progress.locator('.batch-item').filter({hasText:'37床'})).toContainText('尝试 2 次')
  await page.reload();await expect(progress).toContainText('成功 3')
  await progress.locator('.batch-item').filter({hasText:'36床'}).getByRole('button',{name:'查看核对'}).click()
  const drawer=page.getByRole('dialog',{name:'AI病历识别结果 · 待人工核对'})
  await expect(drawer).toContainText('本地模拟识别')
  await drawer.locator('.el-form-item').filter({hasText:'人工核对护理负荷'}).locator('.el-select__wrapper').click()
  await page.getByRole('option',{name:'较轻',exact:true}).click()
  await drawer.getByText('已逐项核对床号、患者身份、事实及护理负荷').click()
  await drawer.getByRole('button',{name:'确认写入对应床位'}).click()
  await expect(drawer).not.toBeVisible()
  await expect(progress.locator('.batch-item').filter({hasText:'36床'})).toContainText('已核对')
  expect(runs).toBe(4)
})

test('批量识别中会话失效只暂停队列，重新登录后继续',async({page})=>{
  expect(process.env.ICU_E2E_ISOLATED).toBe('1');expect(process.env.ICU_AI_ALLOW_MOCK).toBe('true')
  execFileSync(resolve('..','.venv/bin/python'),['manage.py','shell','-c',
    "import os; assert os.environ.get('ICU_E2E_ISOLATED')=='1'; from scheduling.models import CareGroup,Bed; g=CareGroup.objects.get(name='第3组'); [Bed.objects.get_or_create(group=g,number=str(n)) for n in (39,40)]"],{cwd:resolve('..'),env:process.env})
  await page.route('**/api/v1/workspace/**',async route=>{const response=await route.fetch(),body=await response.json();body.data.ai.configured=true;await route.fulfill({response,json:body})})
  let calls=0
  await page.route('**/api/v1/medical-batches/*/run/',async route=>{
    calls++;const response=await route.fetch()
    if(calls===1)await page.context().clearCookies()
    await route.fulfill({response})
  })
  await page.goto('/');await page.getByPlaceholder('请输入账号').fill('group3');await page.getByPlaceholder('请输入密码').fill(process.env.E2E_PASSWORD)
  await page.getByRole('button',{name:'登录',exact:true}).click();await page.getByRole('link',{name:'床位分配',exact:true}).click()
  await page.getByText('批量识别',{exact:true}).click()
  await page.getByText('同时为病历中明确指定的空床导入合成病例',{exact:true}).click()
  const input=page.locator('.ai-box textarea'),text='39床：姓名：合成甲；诊断：肺炎。\n40床：姓名：合成乙；诊断：感染。'
  await input.fill(text)
  const denied=page.waitForResponse(r=>r.url().includes('/api/v1/medical-batches/')&&r.status()===403)
  await page.locator('.ai-box').getByRole('button',{name:'AI识别病历',exact:true}).click()
  const body=await (await denied).json();expect(body.error.code).toBe('not_authenticated')
  await expect(page.getByText(/已暂停后续病例/)).toBeVisible();await expect(input).toHaveValue(text)
  expect(calls).toBe(1)
  await page.getByRole('button',{name:'重新登录当前账号'}).click()
  const login=page.getByRole('dialog',{name:'重新登录当前账号'})
  await login.getByLabel('密码').fill(process.env.E2E_PASSWORD);await login.getByRole('button',{name:'登录',exact:true}).click()
  await expect(login).not.toBeVisible()
  await page.getByRole('button',{name:'继续识别'}).click()
  await expect(page.locator('.batch-progress')).toContainText('成功 2')
  expect(calls).toBe(2)
})
