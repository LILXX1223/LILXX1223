import { test, expect } from '@playwright/test'

// Explicit opt-in only. scripts/run_browser_tests.py --real-ai creates a
// disposable database with seed_demo data and copies only encrypted AI config.
test.skip(process.env.ICU_E2E_REAL_AI !== '1', '仅 --real-ai 隔离联调时调用真实服务')
test.use({actionTimeout:10000})

async function getData(page,path) {
  const response=await page.request.get('/api/v1/'+path)
  expect(response.ok(),`读取 ${path} 失败：${response.status()}`).toBeTruthy()
  return (await response.json()).data
}

async function waitForTask(page,group,kind) {
  let task
  await expect.poll(async()=>{
    const list=await getData(page,`allocation-tasks/?group=${group}&kind=${kind}`)
    task=list[0]
    return task?.status
  },{timeout:150000,intervals:[500,1000,2000]}).toBe('completed')
  const done=new Set(task.events.filter(event=>event.status==='completed').map(event=>event.stage))
  expect([...done].sort()).toEqual([1,2,3,4,5,6,7])
  expect(task.events.find(event=>event.stage===4&&event.status==='completed')?.summary?.model).toBeTruthy()
  expect(task.events.some(event=>event.stage===5&&event.status==='completed')).toBeTruthy()
  return task
}

test('真实 DeepSeek 和 OR-Tools 的合成分组与分床任务不自动发布',async({page},info)=>{
  test.setTimeout(240000)
  await page.goto('/')
  await page.getByPlaceholder('请输入账号').fill('group3')
  await page.getByPlaceholder('请输入密码').fill(process.env.E2E_PASSWORD)
  await page.getByRole('button',{name:'登录',exact:true}).click()
  await expect(page.locator('.sidebar nav a')).toHaveCount(6)
  const before=await getData(page,'workspace/')
  expect(before.ai.configured).toBe(true)
  expect(before.synthetic_demo).toBe(true)
  const group=before.group.id
  const roster=before.nurses.map(({id,subgroup,is_leader})=>({id,subgroup,is_leader}))
  const assignments=before.board.assignments
  const publication=before.board.published

  await page.getByRole('link',{name:'人员信息',exact:true}).click()
  await page.getByRole('button',{name:'AI辅助小组分配',exact:true}).click()
  const grouping=page.getByRole('dialog',{name:'AI辅助小组分配',exact:true})
  await grouping.locator('.rule-panel').getByText('合成资料',{exact:true}).click()
  await grouping.locator('.rule-panel').getByText('已核对资料来源并确认符合外发要求',{exact:true}).click()
  await grouping.getByRole('button',{name:'生成方案',exact:true}).click()
  const groupTask=await waitForTask(page,group,'GROUP_ASSIGNMENT')
  expect(groupTask.parameters.synthetic).toBe(true)
  const groupSolve=groupTask.events.find(event=>event.stage===5&&event.status==='completed')
  expect(groupSolve.summary.plans).toHaveLength(3)
  expect(groupSolve.summary.plans.every(plan=>['optimal','feasible','timeout_feasible','OPTIMAL','FEASIBLE'].includes(plan.status))).toBe(true)
  await expect(grouping.locator('.group-card')).toHaveCount(4,{timeout:15000})
  await grouping.screenshot({path:info.outputPath('grouping.png'),animations:'disabled'})
  const groupChat=grouping.locator('.assistant')
  await expect(groupChat.locator('.analysis-row')).toContainText(`任务 #${groupTask.id}`)
  await expect(groupChat.locator('.analysis-row')).toContainText('7 / 7 已完成')
  await groupChat.getByText('合成资料',{exact:true}).click()
  await groupChat.getByText('已核对内容及外发授权',{exact:true}).click()
  await groupChat.getByPlaceholder('输入问题，避免粘贴不必要的个人或病历信息').fill('请说明当前候选方案的主要核对重点。')
  await groupChat.getByRole('button',{name:'发送',exact:true}).click()
  await expect(groupChat.getByRole('button',{name:'发送',exact:true})).toBeEnabled({timeout:120000})
  await expect(groupChat.locator('.message-row.assistant')).toHaveCount(1)
  await groupChat.getByPlaceholder('输入问题，避免粘贴不必要的个人或病历信息').fill('请继续说明需要人工核实的资格。')
  await groupChat.getByRole('button',{name:'发送',exact:true}).click()
  await expect(groupChat.getByRole('button',{name:'发送',exact:true})).toBeEnabled({timeout:120000})
  await expect(groupChat.locator('.message-row.assistant')).toHaveCount(2)
  await expect(groupChat.getByRole('button',{name:`查看关联任务 #${groupTask.id}`})).toHaveCount(4)
  await groupChat.getByRole('button',{name:'整理为待审核建议'}).last().click()
  await expect(groupChat.getByPlaceholder('描述优化偏好，请勿填写姓名、联系方式或病历详情')).not.toBeEmpty()
  await expect(groupChat.getByRole('button',{name:'生成待审建议'})).toBeVisible()
  await groupChat.getByRole('tab',{name:'对话'}).click()
  await grouping.getByRole('button',{name:'查看优化过程'}).click()
  await expect(grouping.locator('.plans-panel .allocation-progress')).toContainText('7 / 7 已完成')
  const afterGroup=await getData(page,'workspace/')
  expect(afterGroup.nurses.map(({id,subgroup,is_leader})=>({id,subgroup,is_leader}))).toEqual(roster)
  await page.reload()
  await page.getByRole('button',{name:'AI辅助小组分配',exact:true}).click()
  const restoredGroup=page.getByRole('dialog',{name:'AI辅助小组分配',exact:true})
  await restoredGroup.getByRole('button',{name:'查看优化过程'}).click()
  await expect(restoredGroup.locator('.plans-panel .allocation-progress')).toContainText('7 / 7 已完成')
  await restoredGroup.getByRole('button',{name:`查看关联任务 #${groupTask.id}`}).last().click()
  await expect(restoredGroup.locator('.assistant .analysis-row')).toContainText(`历史任务 #${groupTask.id}`)
  await restoredGroup.getByRole('button',{name:'取消',exact:true}).click()

  await page.getByRole('link',{name:'床位分配',exact:true}).click()
  await page.getByRole('button',{name:'智能分配',exact:true}).click()
  const consent=page.getByRole('dialog',{name:'确认AI床位分析资料来源',exact:true})
  await consent.getByText('本次仅使用合成资料',{exact:true}).click()
  await consent.getByText('已核对资料来源及外发授权',{exact:true}).click()
  await consent.getByRole('button',{name:'开始分析',exact:true}).click()
  const bedTask=await waitForTask(page,group,'BED_ASSIGNMENT')
  expect(bedTask.parameters.synthetic).toBe(true)
  expect(bedTask.result.validation.valid).toBe(true)
  expect(['optimal','feasible','timeout_feasible']).toContain(bedTask.result.solver.status)
  await expect(page.getByRole('dialog',{name:'智能床位分配建议'})).toBeVisible({timeout:15000})
  await page.getByRole('dialog',{name:'智能床位分配建议'}).screenshot({path:info.outputPath('bed-recommendation.png'),animations:'disabled'})
  await expect(page.locator('.bed-column')).toContainText('当前显示候选责任')
  const afterBed=await getData(page,'workspace/')
  expect(afterBed.board.assignments).toEqual(assignments)
  expect(afterBed.board.published).toBe(publication)
  await page.getByRole('dialog',{name:'智能床位分配建议'}).getByRole('button',{name:'取消'}).click()
  const bedChat=page.locator('.ai-box .assistant')
  await expect(bedChat.locator('.analysis-row')).toContainText(`任务 #${bedTask.id}`)
  await bedChat.getByText('合成资料',{exact:true}).click()
  await bedChat.getByText('已核对内容及外发授权',{exact:true}).click()
  await bedChat.getByPlaceholder('输入问题，避免粘贴不必要的个人或病历信息').fill('当前候选分配有哪些需要复核的重点？')
  await bedChat.getByRole('button',{name:'发送',exact:true}).click()
  await expect(bedChat.getByRole('button',{name:'发送',exact:true})).toBeEnabled({timeout:120000})
  await expect(bedChat.locator('.message-row.assistant')).toHaveCount(1)
  await expect(bedChat.getByRole('button',{name:`查看关联任务 #${bedTask.id}`})).toHaveCount(2)
  await page.reload()
  await page.getByRole('tab',{name:'AI分配助手',exact:true}).click()
  await page.getByRole('button',{name:'查看本次分配过程'}).click()
  await expect(page.locator('.ai-box > .allocation-progress')).toContainText('7 / 7 已完成')
})
