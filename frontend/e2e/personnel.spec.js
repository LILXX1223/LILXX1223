import { test, expect } from '@playwright/test'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { execFileSync } from 'node:child_process'

async function login(page) {
  await page.goto('/')
  await page.getByPlaceholder('请输入账号').fill('group3')
  await page.getByPlaceholder('请输入密码').fill(process.env.E2E_PASSWORD || 'DemoICU!2026')
  await page.getByRole('button',{name:'登录',exact:true}).click()
  await page.getByRole('link',{name:'人员信息',exact:true}).click()
}

test('第3组管理技能与动态人员页面', async ({ page }, info) => {
  await login(page)
  await expect(page.locator('.personnel-detail')).toContainText('护士详情')
  await expect(page.locator('.personnel-detail')).toContainText('分组归属')
  await expect(page.locator('.personnel-charts canvas')).toHaveCount(3)
  await expect(page.locator('.personnel-metrics canvas')).toHaveCount(2)
  await page.screenshot({path:info.outputPath('personnel-1440.png'),animations:'disabled'})
  await page.getByRole('button',{name:'管理技能',exact:true}).click()
  const win=page.getByRole('dialog',{name:'专科技能项目管理'})
  await expect(win).toContainText('技能项目列表')
  await page.screenshot({path:info.outputPath('skills-dialog.png'),animations:'disabled'})
  const code='E2E_'+Date.now()
  await win.getByRole('button',{name:'新增技能'}).click()
  const form=win.locator('.skill-details')
  await form.getByLabel('技能名称 *').fill('自动化合成技能')
  await form.getByLabel('技能代码 *').fill(code)
  await form.getByLabel('技能分类').fill('联调用')
  await form.getByLabel('技能说明').fill('仅用于自动化测试')
  await form.getByRole('button',{name:'保存技能'}).click()
  const row=win.getByRole('row').filter({hasText:'自动化合成技能'})
  await expect(row).toHaveCount(1)
  await row.getByRole('button',{name:'编辑'}).click()
  await form.getByLabel('技能说明').fill('编辑后的合成技能说明')
  await form.getByRole('button',{name:'保存技能'}).click()
  await expect(row).toContainText('编辑后的合成技能说明')
  await row.getByRole('button',{name:'停用'}).click()
  await expect(row).toContainText('停用')
  await row.getByRole('button',{name:'启用'}).click()
  await expect(row).toContainText('启用')
  await row.getByRole('button',{name:'删除'}).click()
  await page.getByRole('dialog',{name:'确认删除技能'}).getByRole('button',{name:'确认删除'}).click()
  await expect(row).toHaveCount(0)
})

test('新增合成护士、裁剪上传头像并真实删除', async ({ page }) => {
  await login(page)
  const employee='AUTO'+Date.now()
  await page.getByRole('button',{name:'新增护士'}).click()
  const form=page.getByRole('dialog',{name:'新增护士'})
  await form.getByLabel('姓名 *').fill('合成头像测试')
  await form.getByLabel('工号 *').fill(employee)
  const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7+6X0AAAAASUVORK5CYII=','base64')
  await form.locator('input[type=file]').setInputFiles({name:'synthetic.png',mimeType:'image/png',buffer:png})
  await expect(form.locator('canvas')).toBeVisible()
  await expect(form.getByText('正在生成裁剪预览…')).toHaveCount(0)
  await form.getByRole('button',{name:'保存档案'}).click()
  await expect(form).toBeHidden()
  await page.getByPlaceholder('姓名或工号').fill(employee)
  const row=page.getByRole('row').filter({hasText:employee})
  await expect(row).toHaveCount(1)
  await expect(row.locator('img')).toBeVisible()
  const data=await (await page.request.get('/api/v1/workspace/')).json()
  const nurse=data.data.nurses.find(n=>n.employee_id===employee)
  expect(nurse.subgroup).toBeNull();expect(nurse.avatar_url).toBeTruthy()
  expect((await page.request.get(nurse.avatar_url)).ok()).toBeTruthy()
  await row.getByRole('button',{name:'删除',exact:true}).click()
  await page.getByRole('dialog',{name:'确认删除护士'}).getByRole('button',{name:'确认删除'}).click()
  await expect(row).toHaveCount(0)
  expect((await page.request.get(nurse.avatar_url)).status()).toBe(404)
})

test('Excel合成数据五步核对与批量导入', async ({ page }) => {
  const directory=mkdtempSync(join(tmpdir(),'icu-excel-ui-')),file=join(directory,'synthetic.xlsx'),employee='XLS'+Date.now()
  try{
    execFileSync(resolve('..','.venv/bin/python'),['-c',
      'import sys; from openpyxl import Workbook; w=Workbook(); s=w.active; s.append(["姓名","工号","护理层级","年资","技能","带教与协作能力","病情评估能力能级"]); s.append(["合成导入测试",sys.argv[2],"N2",5,"CRRT","带教记录待核实",3]); w.save(sys.argv[1])',file,employee])
    await login(page)
    await page.getByRole('button',{name:'Excel智能导入'}).click()
    const win=page.getByRole('dialog',{name:'Excel智能导入护士资料'})
    await win.locator('input[type=file]').setInputFiles(file)
    await expect(win).toContainText('synthetic.xlsx')
    await win.getByRole('button',{name:'下一步',exact:true}).click()
    await expect(win).toContainText('Excel原始字段')
    await win.getByRole('button',{name:'保存映射并继续'}).click()
    await expect(win).toContainText(employee)
    await win.getByRole('button',{name:'下一步',exact:true}).click()
    await win.getByRole('button',{name:'核对修改'}).click()
    await expect(win).toContainText('系统现有资料')
    await win.getByRole('button',{name:'保存核对结果'}).click()
    await expect(win).toContainText('新增 1')
    await win.getByText('已逐条核对原文、匹配对象和拟保存内容').click()
    await win.getByRole('button',{name:'确认导入',exact:true}).click()
    await expect(win).toContainText('导入结果')
    await win.getByRole('button',{name:'关闭',exact:true}).click()
    await page.getByPlaceholder('姓名或工号').fill(employee)
    const row=page.locator('.personnel-left').getByRole('row').filter({hasText:employee});await expect(row).toContainText('待分组')
    const workspace=(await (await page.request.get('/api/v1/workspace/')).json()).data
    const nurse=workspace.nurses.find(n=>n.employee_id===employee)
    expect(nurse.teaching_collaboration).toBe('带教记录待核实');expect(nurse.assessment_level).toBe(3)
    expect(nurse.skills).toContain('CRRT');expect(nurse.certifications).toHaveLength(0)
    await row.getByRole('button',{name:'删除',exact:true}).click()
    await page.getByRole('dialog',{name:'确认删除护士'}).getByRole('button',{name:'确认删除'}).click()
    await expect(row).toHaveCount(0)
  } finally {
    // Only remove the synthetic nurse created by this test if an assertion fails mid-flow.
    try {
      const workspace=await page.request.get('/api/v1/workspace/')
      if(workspace.ok()){
        const data=(await workspace.json()).data
        const nurse=data.nurses.find(n=>n.employee_id===employee&&n.name==='合成导入测试')
        if(nurse){const csrf=(await page.context().cookies()).find(c=>c.name==='csrftoken')?.value||''
          await page.request.delete(`/api/v1/nurses/${nurse.id}/`,{headers:{'X-CSRFToken':csrf},data:{group:data.group.id,revision:nurse.revision}})}
      }
    } catch {}
    rmSync(directory,{recursive:true,force:true})
  }
})

test('AI未配置时分组窗口明确阻止生成', async ({ page }) => {
  await login(page)
  const workspace=(await (await page.request.get('/api/v1/workspace/')).json()).data
  test.skip(workspace.ai.configured,'当前已配置真实AI，跳过未配置状态测试。')
  await page.getByRole('button',{name:'AI辅助小组分配'}).click()
  const win=page.getByRole('dialog',{name:'AI辅助小组分配'})
  await expect(win).toContainText('AI服务未配置')
  await expect(win).toContainText('能力能级的分档含义尚未确认')
  await expect(win.getByRole('button',{name:'生成方案'})).toBeDisabled()
  await expect(win).toContainText('默认3年仅作演示')
})
