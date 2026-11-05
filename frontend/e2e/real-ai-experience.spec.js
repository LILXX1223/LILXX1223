import { test, expect } from '@playwright/test'

test.skip(process.env.ICU_E2E_REAL_AI !== '1','仅 --real-ai 隔离联调时调用真实服务')
test('真实模型提炼已确认反馈，两类经验在下一次优化中生效',async({page})=>{
  test.setTimeout(360000)
  await page.goto('/')
  await page.getByPlaceholder('请输入账号').fill('group3')
  await page.getByPlaceholder('请输入密码').fill(process.env.E2E_PASSWORD)
  await page.getByRole('button',{name:'登录',exact:true}).click()
  await expect(page.getByRole('link',{name:'人员信息',exact:true})).toBeVisible()
  const csrf=decodeURIComponent((await page.context().cookies()).find(c=>c.name==='csrftoken').value)
  const headers={'X-CSRFToken':csrf}
  async function get(path){
    const response=await page.request.get('/api/v1/'+path)
    expect(response.ok(),`${path}: ${response.status()}`).toBeTruthy()
    return (await response.json()).data
  }
  async function post(path,data){
    const response=await page.request.post('/api/v1/'+path,{headers,data,timeout:90000})
    const body=await response.json()
    expect(response.ok(),`${path}: ${response.status()} ${body.error?.message||''}`).toBeTruthy()
    return body.data
  }
  async function waitTask(id,group){
    let task
    await expect.poll(async()=>{task=await get(`allocation-tasks/${id}/?group=${group}`);return task.status},
      {timeout:150000,intervals:[500,1000,2000]}).toBe('completed')
    return task
  }
  const initial=await get('workspace/'),group=initial.group.id
  const parameters={group,kind:'GROUP_ASSIGNMENT',group_count:4,group_sizes:[3,3,3,3],min_leaders:1,
    mentor_ids:[],locked:{},junior_years:2,effective_date:initial.today,synthetic:true,consent:true}
  const first=await post('allocation-tasks/',parameters)
  const execution=await waitTask(first.id,group)
  const task=await get(`grouping/${execution.result.grouping_task_id}/?group=${group}`)
  const plan=task.plans[0], assignments={...plan.assignments}
  const members=task.participants.filter(n=>!n.is_leader)
  const left=members[0],right=members.find(n=>assignments[String(n.id)]!==assignments[String(left.id)])
  expect(right).toBeTruthy()
  ;[assignments[String(left.id)],assignments[String(right.id)]]=[assignments[String(right.id)],assignments[String(left.id)]]
  const review=await post(`grouping/${task.id}/review/`,{group,revision:task.revision,assignments,leaders:plan.leaders,
    reference_assignments:plan.assignments,reference_leaders:plan.leaders,adjustment_reason:'专科技能覆盖不足'})
  await post(`grouping/${task.id}/confirm/`,{group,revision:review.revision,assignments,leaders:plan.leaders,confirmed:true})
  const groupAdvice=await post('experiences/analyze-feedback/',{group,business_type:'GROUP_ASSIGNMENT',synthetic:true,consent:true})
  expect(groupAdvice.status).toBe('pending');expect(groupAdvice.source).toBe('ai');expect(groupAdvice.source_details.count).toBeGreaterThan(0)
  const approvedGroup=await post(`experiences/${groupAdvice.id}/action/`,{group,revision:groupAdvice.revision,action:'adopt',confirmed:true})
  const next=await post('allocation-tasks/',parameters),nextResult=await waitTask(next.id,group)
  const groupPlan=await get(`grouping/${nextResult.result.grouping_task_id}/?group=${group}`)
  expect(groupPlan.parameters.applied_experiences.some(e=>e.id===groupAdvice.id&&e.revision===approvedGroup.revision)).toBe(true)
  expect(groupPlan.plans[0].normalization[groupAdvice.target].coefficient).toBe(Math.round(groupAdvice.weight*10))
  expect(groupPlan.plans.every(p=>p.validation.valid)).toBe(true)

  // Create a real synthetic manual adjustment, then explicitly publish it in
  // this disposable database before asking the model to analyze the feedback.
  let board=(await get('workspace/')).board,adjusted=false
  const ids=Object.keys(board.assignments)
  for(let i=0;i<ids.length&&!adjusted;i++)for(let j=i+1;j<ids.length&&!adjusted;j++){
    const a=ids[i],b=ids[j]
    if(board.assignments[a]===board.assignments[b])continue
    const assignments={...board.assignments,[a]:board.assignments[b],[b]:board.assignments[a]}
    const response=await page.request.post('/api/v1/beds/adjust/',{headers,data:{group,id:board.id,
      revision:board.revision,fingerprint:board.fingerprint,assignments,reason:'护理连续性考虑'}})
    if(response.ok())adjusted=true
    else expect(response.status()).toBe(422)
  }
  expect(adjusted,'合成床位需存在可通过约束的换床调整').toBe(true)
  board=(await get('workspace/')).board
  await post('beds/publish/',{group,id:board.id,revision:board.revision,fingerprint:board.fingerprint,confirm_three:true})
  const bedAdvice=await post('experiences/analyze-feedback/',{group,business_type:'BED_ASSIGNMENT',synthetic:true,consent:true})
  expect(bedAdvice.status).toBe('pending');expect(bedAdvice.source).toBe('ai');expect(bedAdvice.source_details.count).toBeGreaterThan(0)
  const approvedBed=await post(`experiences/${bedAdvice.id}/action/`,{group,revision:bedAdvice.revision,action:'adopt',confirmed:true})
  board=(await get('workspace/')).board
  const bedTask=await post('allocation-tasks/',{group,kind:'BED_ASSIGNMENT',id:board.id,revision:board.revision,
    fingerprint:board.fingerprint,synthetic:true,consent:true})
  const bedResult=await waitTask(bedTask.id,group)
  expect(bedResult.result.applied_experiences.some(e=>e.id===bedAdvice.id&&e.revision===approvedBed.revision)).toBe(true)
  expect(bedResult.result.effective_weights[bedAdvice.target]).toBe(bedAdvice.weight)
  expect(bedResult.result.validation.valid).toBe(true)
})
