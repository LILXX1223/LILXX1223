import { test, expect } from '@playwright/test'

// Only the isolated harness may enable this test; all patient and nurse data
// in its temporary SQLite database are synthetic.
test.skip(process.env.ICU_E2E_REAL_AI !== '1', '仅隔离的真实 AI 联调执行')
test('两类助手的本次确认产生新任务，保留候选与正式状态', async ({ page }) => {
  test.setTimeout(480000)
  await page.goto('/')
  await page.getByPlaceholder('请输入账号').fill('group3')
  await page.getByPlaceholder('请输入密码').fill(process.env.E2E_PASSWORD)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('link', { name: '人员信息', exact: true })).toBeVisible()
  const csrf = decodeURIComponent((await page.context().cookies()).find(c => c.name === 'csrftoken').value)
  const headers = { 'X-CSRFToken': csrf }
  async function get(path) {
    const response = await page.request.get('/api/v1/' + path)
    expect(response.ok(), `${path}: ${response.status()}`).toBeTruthy()
    return (await response.json()).data
  }
  async function post(path, data) {
    const response = await page.request.post('/api/v1/' + path, { headers, data })
    expect(response.ok(), `${path}: ${response.status()}`).toBeTruthy()
    return (await response.json()).data
  }
  const initial = await get('workspace/'), group = initial.group.id
  const beforeNurses = initial.nurses.map(({ id, subgroup, is_leader }) => ({ id, subgroup, is_leader }))
  const beforeAssignments = initial.board.assignments
  const beforePublication = initial.board.published
  const groupAdvice = await post('experiences/', { group, business_type: 'GROUP_ASSIGNMENT',
    title: '合成分组本次建议', description: '合成测试：本次提高技能互补', target: 'skill_coverage', weight: 0.4 })
  const bedAdvice = await post('experiences/', { group, business_type: 'BED_ASSIGNMENT',
    title: '合成分床本次建议', description: '合成测试：本次提高负荷均衡', target: 'load_balance', weight: 0.4 })
  async function waitRound(kind, previous = 0) {
    let task
    await expect.poll(async () => {
      const list = await get(`allocation-tasks/?group=${group}&kind=${kind}`)
      task = list.find(item => item.id > previous)
      return task?.status
    }, { timeout: 150000, intervals: [500, 1000, 2000] }).toBe('completed')
    expect(new Set(task.events.filter(e => e.status === 'completed').map(e => e.stage)).size).toBe(7)
    return task
  }
  function applied(task, id) {
    const stage = task.events.find(e => e.stage === 3 && e.status === 'completed')
    expect(stage.summary.applied_experiences.some(e => e.id === id && e.one_time)).toBe(true)
    expect(task.events.some(e => e.stage === 5 && e.status === 'completed')).toBe(true)
  }

  await page.getByRole('link', { name: '人员信息', exact: true }).click()
  await page.getByRole('button', { name: 'AI辅助小组分配', exact: true }).click()
  const grouping = page.getByRole('dialog', { name: 'AI辅助小组分配', exact: true })
  await grouping.locator('.rule-panel').getByText('合成资料', { exact: true }).click()
  await grouping.locator('.rule-panel').getByText('已核对资料来源并确认符合外发要求', { exact: true }).click()
  await grouping.getByRole('button', { name: '生成方案', exact: true }).click()
  const groupA = await waitRound('GROUP_ASSIGNMENT')
  await expect(grouping.locator('.group-card')).toHaveCount(4, { timeout: 15000 })
  const groupAssistant = grouping.locator('.assistant')
  await groupAssistant.getByRole('tab', { name: '经验', exact: true }).click()
  const groupCard = groupAssistant.locator('article.record').filter({ hasText: '合成分组本次建议' })
  await groupCard.getByRole('button', { name: '本次采用并重新优化', exact: true }).click()
  await page.getByRole('dialog', { name: '重新生成方案' }).getByRole('button', { name: '确定', exact: true }).click()
  const groupB = await waitRound('GROUP_ASSIGNMENT', groupA.id)
  applied(groupB, groupAdvice.id)
  expect(groupB.parameters.previous_task_id).toBe(groupA.id)
  const groupComparison = await get(`allocation-tasks/compare/?group=${group}&left=${groupA.id}&right=${groupB.id}`)
  expect(groupComparison.rounds.map(row => row.task_id)).toEqual([groupA.id, groupB.id])
  expect((await get('workspace/')).nurses.map(({ id, subgroup, is_leader }) => ({ id, subgroup, is_leader }))).toEqual(beforeNurses)
  await grouping.getByRole('button', { name: '取消', exact: true }).click()

  await page.getByRole('link', { name: '床位分配', exact: true }).click()
  async function consentBed() {
    const consent = page.getByRole('dialog', { name: '确认AI床位分析资料来源', exact: true })
    await consent.getByText('本次仅使用合成资料', { exact: true }).click()
    await consent.getByText('已核对资料来源及外发授权', { exact: true }).click()
    await consent.getByRole('button', { name: '开始分析', exact: true }).click()
  }
  await page.getByRole('button', { name: '智能分配', exact: true }).click()
  await consentBed()
  const bedA = await waitRound('BED_ASSIGNMENT')
  const preview = page.getByRole('dialog', { name: '智能床位分配建议' })
  await expect(preview).toBeVisible({ timeout: 15000 })
  await preview.getByRole('button', { name: '取消', exact: true }).click()
  const bedAssistant = page.locator('.ai-box .assistant')
  await bedAssistant.getByRole('tab', { name: '经验', exact: true }).click()
  const bedCard = bedAssistant.locator('article.record').filter({ hasText: '合成分床本次建议' })
  await bedCard.getByRole('button', { name: '本次采用并重新优化', exact: true }).click()
  await consentBed()
  const bedB = await waitRound('BED_ASSIGNMENT', bedA.id)
  applied(bedB, bedAdvice.id)
  expect(bedB.parameters.previous_task_id).toBe(bedA.id)
  const bedComparison = await get(`allocation-tasks/compare/?group=${group}&left=${bedA.id}&right=${bedB.id}`)
  expect(bedComparison.rounds.map(row => row.task_id)).toEqual([bedA.id, bedB.id])
  const after = await get('workspace/')
  expect(after.board.assignments).toEqual(beforeAssignments)
  expect(after.board.published).toBe(beforePublication)
})
