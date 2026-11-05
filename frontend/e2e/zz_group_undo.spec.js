import { test, expect } from '@playwright/test'

// The isolated runner supplies a disposable database. This intentionally runs
// last because it exercises the real clear action on its synthetic nurses.
test('撤销正式小组：预览、二次确认及历史排班保留', async ({ page }, info) => {
  await page.goto('/')
  await page.getByPlaceholder('请输入账号').fill('group3')
  await page.getByPlaceholder('请输入密码').fill(process.env.E2E_PASSWORD || 'DemoICU!2026')
  await page.getByRole('button',{name:'登录',exact:true}).click()
  await page.getByRole('link',{name:'人员信息',exact:true}).click()
  const readState=async()=>{
    const response=await page.request.get('/api/v1/grouping/formal/')
    expect(response.ok()).toBeTruthy()
    return (await response.json()).data
  }
  const before=await readState()
  const getPublications=async()=>{
    const response=await page.request.get('/api/v1/workspace/')
    expect(response.ok()).toBeTruthy()
    return (await response.json()).data.publications
  }
  const beforePublications=await getPublications()
  expect(beforePublications.length).toBeGreaterThan(0)
  expect(before.assigned_count).toBeGreaterThan(0)
  await page.getByRole('button',{name:'撤销当前分组'}).click()
  const win=page.getByRole('dialog',{name:'撤销当前小组分配',exact:true})
  await expect(win).toContainText('受影响人员预览')
  await expect(win).toContainText(`（${before.assigned_count} 人）`)
  await page.screenshot({path:info.outputPath('group-undo-preview.png'),animations:'disabled'})
  await win.getByRole('button',{name:'清空当前小组',exact:true}).click()
  const confirm=page.getByRole('dialog',{name:'再次确认',exact:true})
  await expect(confirm).toContainText('已发布历史排班保持不变')
  await confirm.getByRole('button',{name:'取消',exact:true}).click()
  expect((await readState()).fingerprint).toBe(before.fingerprint)
  await win.getByRole('button',{name:'清空当前小组',exact:true}).click()
  await confirm.getByRole('button',{name:'确认执行'}).click()
  await expect(win).toBeHidden()
  const after=await readState()
  expect(after.assigned_count).toBe(0)
  expect(after.current_version).toBeTruthy()
  expect(after.members.map(n=>[n.id,n.is_leader])).toEqual(before.members.map(n=>[n.id,n.is_leader]))
  expect(await getPublications()).toEqual(beforePublications)
  await page.reload()
  await expect(page.getByRole('button',{name:'撤销当前分组'})).toBeDisabled()
  await expect(page.getByRole('row').filter({hasText:before.members[0].employee_id}).first()).toContainText('未分组')

  // Exercise the UI branch with an available predecessor. The actual restore,
  // including rule and qualification checks, is covered by the Django test.
  await page.route(/\/api\/v1\/grouping\/formal\/\?/,async route=>{
    const response=await route.fetch(), body=await response.json()
    body.data.can_restore=true
    body.data.previous_version=before.current_version||1
    body.data.previous_members=before.members
    body.data.restore_issues=[]
    await route.fulfill({response,json:body})
  })
  await page.reload()
  await expect(page.getByRole('button',{name:'撤销当前分组'})).toBeEnabled()
  await page.getByRole('button',{name:'撤销当前分组'}).click()
  await expect(page.getByRole('radio',{name:/恢复上一正式分组/})).toBeEnabled()
  await page.getByRole('dialog',{name:'撤销当前小组分配'}).getByRole('button',{name:'取消',exact:true}).click()
  await page.getByRole('link',{name:'排班管理',exact:true}).click()
  const qualified=after.members.find(n=>n.is_leader)
  const row=page.locator('.schedule-matrix tbody tr').filter({hasText:qualified.name}).first()
  await expect(row.locator('td').nth(3)).toHaveText('未分组')
})
