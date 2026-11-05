import { test, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'

test.skip(process.env.ICU_E2E_REAL_AI !== '1','仅真实AI隔离联调时运行')
test('真实模型请求期间显示服务端识别中状态',async({page})=>{
  test.setTimeout(240000)
  expect(process.env.ICU_E2E_ISOLATED).toBe('1')
  execFileSync(resolve('..','.venv/bin/python'),['manage.py','shell','-c',
    "import os; assert os.environ.get('ICU_E2E_ISOLATED')=='1'; from scheduling.models import CareGroup,Bed; g=CareGroup.objects.get(name='第3组'); Bed.objects.get_or_create(group=g,number='36')"],{cwd:resolve('..'),env:process.env})
  await page.goto('/');await page.getByPlaceholder('请输入账号').fill('group3');await page.getByPlaceholder('请输入密码').fill(process.env.E2E_PASSWORD)
  await page.getByRole('button',{name:'登录',exact:true}).click();await page.getByRole('link',{name:'床位分配',exact:true}).click()
  await page.getByText('批量识别',{exact:true}).click();await page.getByText('同时为病历中明确指定的空床导入合成病例',{exact:true}).click()
  await page.locator('.ai-box textarea').fill('36床：姓名：合成患者；性别：男；年龄：55岁；确定诊断：肺炎；目前持续机械通气。')
  await page.locator('.ai-box').getByRole('button',{name:'AI识别病历',exact:true}).click()
  const progress=page.locator('.batch-progress')
  await expect(progress).toContainText('识别中 1',{timeout:15000})
  await expect(progress).toContainText('成功 1',{timeout:180000})
  await expect(progress).toContainText('36床 · 成功 · 待核对')
})
