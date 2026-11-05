import { test, expect } from '@playwright/test'

// Runs against the local synthetic demonstration database. Never use real patient data.
async function login(page, username = 'group3') {
  await page.goto('/')
  await page.getByPlaceholder('请输入账号').fill(username)
  await page.getByPlaceholder('请输入密码').fill(process.env.E2E_PASSWORD || 'DemoICU!2026')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.locator('.sidebar nav a')).toHaveCount(6)
}
async function data(page, path) {
  const response = await page.request.get('/api/v1/' + path)
  expect(response.ok()).toBeTruthy()
  return (await response.json()).data
}
async function action(page, path, click) {
  const pending = page.waitForResponse(r => r.url().includes('/api/v1/' + path) && r.request().method() === 'POST')
  await click()
  const response = await pending
  const body = await response.json()
  expect(response.ok(), JSON.stringify(body)).toBeTruthy()
  return body.data
}
const dialog = (page, name) => page.getByRole('dialog', { name, exact: true })
async function stableScreenshot(page, info, name, drawer=false) {
  if (!drawer) await expect(page.locator('.el-overlay:visible')).toHaveCount(0)
  await expect(page.locator('.el-message:visible')).toHaveCount(0)
  await page.evaluate(() => { window.scrollTo(0, 0); return document.fonts.ready })
  await page.screenshot({ path: info.outputPath(name), animations: 'disabled', fullPage: false })
}

test('管理账号：人员维护→排班发布→人工核对→床位发布→统计闭环', async ({ page }, info) => {
  const crashes = []
  page.on('pageerror', e => crashes.push(e.message))
  await login(page)
  await expect(page.locator('.topbar')).toContainText('第3组账号')

  await page.getByRole('link', { name: '人员信息', exact: true }).click()
  await page.getByRole('button', { name: '编辑', exact: true }).first().click()
  const nurseDialog = dialog(page, '编辑护士信息')
  await expect(nurseDialog).toBeVisible()
  await nurseDialog.getByPlaceholder('备注').fill('合成演示人员，浏览器端到端联调已核对。')
  await nurseDialog.getByRole('button', { name: '保存档案' }).click()
  await expect(nurseDialog).toBeHidden()
  await page.getByRole('button', { name: '查看', exact: true }).first().click()
  await expect(page.locator('.personnel-detail')).toContainText('浏览器端到端联调已核对')

  await page.getByRole('link', { name: '排班管理', exact: true }).click()
  await expect(page.getByRole('tab')).toHaveCount(4)
  await expect(page.locator('.page-heading .actions > button')).toHaveCount(5)
  await page.getByRole('button', { name: '智能生成排班', exact: true }).click()
  await dialog(page, '智能生成排班').getByRole('button', { name: '开始生成' }).click()
  const overwrite = dialog(page, '重新生成确认')
  try {
    await overwrite.waitFor({ state: 'visible', timeout: 3000 })
    await overwrite.getByRole('button', { name: '确定', exact: true }).click()
  } catch {
    // 无既有草稿时不出现覆盖确认
  }
  await expect(dialog(page, '排班校验')).toContainText('校验通过')
  await dialog(page, '排班校验').getByRole('button', { name: '返回修改' }).click()
  await stableScreenshot(page, info, 'schedule-1440.png')
  await page.getByRole('button', { name: '审核并发布', exact: true }).click()
  await expect(dialog(page, '审核并发布')).toContainText('校验通过')
  const schedule = await action(page, 'schedules/publish/', () => dialog(page, '审核并发布').getByRole('button', { name: '确认发布', exact: true }).click())
  await expect(dialog(page, '排班发布成功')).toBeVisible()
  await dialog(page, '排班发布成功').getByRole('button', { name: '前往总览查看' }).click()
  await page.getByRole('button', { name: '查看完整排班 →' }).click()
  await expect(page.locator('h1')).toContainText('已发布排班')
  await expect(page.locator('.schedule-matrix')).toBeVisible()

  await page.getByRole('link', { name: '床位分配', exact: true }).click()
  await page.getByRole('button', { name: '同步当班护士', exact: true }).click()
  await dialog(page, '同步当班护士').getByRole('button', { name: '确认同步' }).click()
  await expect(dialog(page, '同步当班护士')).toBeHidden()
  await expect(page.locator('.ai-box')).toContainText('AI服务未配置')
  await expect(page.getByRole('button', { name: 'AI识别病历', exact: true }).last()).toBeDisabled()
  const before = await data(page, 'workspace/')
  await page.getByRole('button', { name: '1床操作', exact: true }).click()
  await page.getByRole('menuitem', { name: '编辑患者信息', exact: true }).click()
  const review = dialog(page, '1床 · 人工维护患者信息')
  await review.getByLabel('患者名称 *').fill('合成患者一（人工核对）')
  await expect(review.getByRole('button', { name: '保存', exact: true })).toBeDisabled()
  await review.getByText('已核对患者信息，确认为合成演示资料').click()
  await review.getByRole('button', { name: '保存', exact: true }).click()
  await expect(review).toBeHidden()
  const after = await data(page, 'workspace/')
  expect(after.board.beds[0].patient.revision).toBe(before.board.beds[0].patient.revision+1)
  expect(after.board.beds[0].patient.name).toBe('合成患者一（人工核对）')

  await page.getByRole('button', { name: '智能分配', exact: true }).click()
  await expect(dialog(page, '智能床位分配建议')).toContainText('校验通过')
  await dialog(page, '智能床位分配建议').getByRole('button', { name: '应用推荐方案' }).click()
  await expect(dialog(page, '智能床位分配建议')).toBeHidden()
  const board = (await data(page, 'workspace/')).board
  const occupied = board.beds.filter(b => b.patient)
  for (const b of occupied) {
    const nurse = board.nurses.find(n => n.id === board.assignments[String(b.id)])
    const card = page.locator('.bed-card').filter({ has: page.locator('header b', { hasText: new RegExp('^' + b.number + '床$') }) })
    await expect(card.locator('.bed-nurse')).toContainText(nurse.name)
    const nurseCard = page.locator('.nurse-card').filter({ has: page.locator('header b', { hasText: nurse.name }) })
    await expect(nurseCard).toContainText(b.number + '床')
  }
  await page.getByRole('button', { name: '人工调整', exact: true }).click()
  const adjustment = dialog(page, '人工调整责任护士')
  // Selecting an existing valid responsibility still exercises revision-safe manual save and audit.
  await adjustment.locator('.el-form-item').filter({ hasText: '新责任护士' }).locator('.el-select').click()
  const firstBed = occupied[0], assignedNurse = board.nurses.find(n => n.id === board.assignments[String(firstBed.id)])
  await page.getByRole('option', { name: new RegExp(assignedNurse.name) }).click()
  await adjustment.getByRole('button', { name: '保存并校验' }).click()
  await expect(dialog(page, '分配校验')).toContainText('校验通过')
  await dialog(page, '分配校验').getByRole('button', { name: '返回', exact: true }).click()
  await page.getByRole('button', { name: '确认并发布', exact: true }).click()
  const publish = dialog(page, '确认并发布床位分配')
  await expect(publish).toContainText('校验通过')
  const ack = publish.locator('.el-checkbox', { hasText: '已确认三床组合' })
  if (await ack.count()) await ack.click()
  await action(page, 'beds/publish/', () => publish.getByRole('button', { name: '确认发布', exact: true }).click())
  await expect(publish).toBeHidden()
  await page.setViewportSize({ width: 1920, height: 1080 })
  await stableScreenshot(page, info, 'beds-1920.png')
  await page.setViewportSize({ width: 1366, height: 768 })
  await stableScreenshot(page, info, 'beds-1366.png')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()

  await page.getByRole('link', { name: '工作量统计', exact: true }).click()
  await expect(page.locator('main .kpis > .kpi')).toHaveCount(2)
  await expect(page.locator('main canvas')).toHaveCount(0)
  const workload = await data(page, 'workload/?period=week')
  expect(workload.patients).toBeGreaterThanOrEqual(occupied.length)
  expect(workload.rows.reduce((sum, row) => sum + row.total_shifts, 0)).toBe(workload.total_shifts)
  for (const row of workload.rows) expect(row.daily.reduce((sum, d) => sum + d.hours, 0)).toBe(row.hours)
  await page.getByRole('button', { name: '查看详情', exact: true }).first().click()
  await expect(dialog(page, '护士工作量详情').locator('canvas')).toBeVisible()
  await stableScreenshot(page, info, 'workload-drawer.png', true)
  expect(crashes).toEqual([])
  await info.attach('workflow-evidence', { body: JSON.stringify({ schedule, bedPublication: (await data(page, 'workspace/')).board.published, workload }, null, 2), contentType: 'application/json' })
})

test('管理账号代录申请；旧护士账号停用，护士长只读', async ({ page }) => {
  await login(page, 'group3')
  await page.getByRole('link', { name: '排班管理', exact: true }).click()
  await page.getByRole('tab', { name: '调班申请', exact: true }).click()
  await page.getByRole('button', { name: '代录申请' }).click()
  const request = dialog(page, '代录调班申请')
  await request.getByLabel('申请理由').fill('合成演示申请：管理员代录测试。')
  const created=await action(page, 'requests/create/', () => request.getByRole('button', { name: '提交申请', exact: true }).click())
  expect(created.submitted_by).toBe('group3')
  await expect(request).toBeHidden()
  await page.getByRole('button', { name: '退出登录' }).click()
  await page.getByPlaceholder('请输入账号').fill('nurse001')
  await page.getByPlaceholder('请输入密码').fill(process.env.E2E_PASSWORD || 'DemoICU!2026')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.locator('.login-card')).toContainText('账号或密码错误')
  await page.getByPlaceholder('请输入账号').fill('head_nurse')
  await page.getByPlaceholder('请输入密码').fill(process.env.E2E_PASSWORD || 'DemoICU!2026')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.locator('.sidebar nav a')).toHaveCount(6)
  await page.getByRole('link', { name: '床位分配', exact: true }).click()
  await expect(page.getByRole('button', { name: '确认并发布', exact: true })).toHaveCount(0)
  await page.getByRole('link', { name: '系统设置', exact: true }).click()
  await expect(page.locator('h1')).toContainText('系统设置')
})

test('六个页面在两种桌面尺寸可访问，无全页横向溢出', async ({ page }, info) => {
  const crashes=[];page.on('pageerror',e=>crashes.push(e.message))
  await login(page)
  for (const viewport of [{width:1920,height:1080},{width:1366,height:768}]) {
    await page.setViewportSize(viewport)
    for (const [route,name] of [['overview','总览'],['personnel','人员信息'],['scheduling','排班管理'],['beds','床位分配'],['workload','工作量统计'],['settings','系统设置']]) {
      await page.getByRole('link',{name,exact:true}).click()
      await expect(page.locator('h1')).toContainText(name)
      await expect(page.locator('.el-loading-mask:visible')).toHaveCount(0)
      expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy()
      await stableScreenshot(page,info,`${route}-${viewport.width}.png`)
    }
  }
  await page.getByRole('link',{name:'床位分配',exact:true}).click()
  await page.getByRole('button',{name:'1床操作',exact:true}).click()
  await page.getByRole('menuitem',{name:'查看患者详情',exact:true}).click()
  const details=page.getByRole('dialog',{name:'1床 · 患者详情',exact:true})
  await expect(details).toContainText('患者资料版本')
  await details.locator('.el-select').click()
  await page.locator('.el-select-dropdown:visible .el-select-dropdown__item').first().click()
  await expect(details).toContainText('历史版本只读')
  expect(crashes).toEqual([])
})
