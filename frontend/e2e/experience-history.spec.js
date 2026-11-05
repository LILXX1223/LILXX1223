import { test, expect } from '@playwright/test'

test('经验历史：人工确认恢复、版本追溯及业务类型隔离',async({page})=>{
  await page.goto('/')
  await page.getByPlaceholder('请输入账号').fill('group3')
  await page.getByPlaceholder('请输入密码').fill(process.env.E2E_PASSWORD)
  await page.getByRole('button',{name:'登录',exact:true}).click()
  await expect(page.getByRole('link',{name:'人员信息',exact:true})).toBeVisible()
  const cookies=await page.context().cookies()
  const csrf=decodeURIComponent(cookies.find(c=>c.name==='csrftoken').value)
  const headers={'X-CSRFToken':csrf}
  const workspace=await page.request.get('/api/v1/workspace/')
  const group=(await workspace.json()).data.group.id
  async function post(path,data){
    const response=await page.request.post('/api/v1/'+path,{headers,data:{group,...data}})
    expect(response.ok(),`${path}: ${response.status()}`).toBeTruthy()
    return (await response.json()).data
  }
  const item=await post('experiences/',{business_type:'GROUP_ASSIGNMENT',title:'隔离测试技能偏好',
    description:'合成偏好，仅用于测试版本恢复',target:'skill_coverage',weight:0.7})
  await post(`experiences/${item.id}/action/`,{revision:1,action:'adopt',confirmed:true})
  await post(`experiences/${item.id}/action/`,{revision:2,action:'pause',confirmed:true})
  const bed=await post('experiences/',{business_type:'BED_ASSIGNMENT',title:'隔离测试分床偏好',
    description:'合成偏好，仅用于测试类型隔离',target:'load_balance',weight:0.8})
  await page.getByRole('link',{name:'人员信息',exact:true}).click()
  await page.getByRole('button',{name:'AI辅助小组分配',exact:true}).click()
  const dialog=page.getByRole('dialog',{name:'AI辅助小组分配',exact:true})
  const assistant=dialog.locator('.assistant')
  await assistant.getByRole('tab',{name:'经验',exact:true}).click()
  const card=assistant.locator('article.record').filter({hasText:'隔离测试技能偏好'})
  await expect(card).toContainText('已暂停')
  await expect(assistant).not.toContainText('隔离测试分床偏好')
  await card.locator('summary').click()
  const activeVersion=card.locator('details > div').filter({hasText:/版本 2\s*·\s*已长期采用/})
  await expect(activeVersion).toContainText('已长期采用')
  await activeVersion.getByRole('button',{name:'恢复此版本'}).click()
  const restoreResponse=page.waitForResponse(response=>response.url().includes(`/experiences/${item.id}/action/`)&&response.request().method()==='POST')
  await page.getByRole('dialog',{name:'确认经验状态'}).getByRole('button',{name:'确定',exact:true}).click()
  const restored=await restoreResponse
  expect(restored.ok(),`恢复经验失败：${restored.status()} ${(await restored.text())}`).toBeTruthy()
  await expect(card).toContainText('已长期采用')
  await expect(card.locator('summary')).toContainText('当前 4')
  await card.getByRole('button',{name:'编辑',exact:true}).click()
  await assistant.getByPlaceholder('经验说明').fill('合成测试：修改后需重新审核')
  await assistant.getByRole('button',{name:'保存待审修改'}).click()
  await page.getByRole('dialog',{name:'确认修改经验'}).getByRole('button',{name:'确定',exact:true}).click()
  await expect(card).toContainText('待审核')
  await expect(card).toContainText('合成测试：修改后需重新审核')
  const history=await page.request.get(`/api/v1/experiences/${item.id}/versions/?group=${group}`)
  expect((await history.json()).data.map(v=>v.revision)).toEqual([5,4,3,2,1])
  const other=await page.request.get(`/api/v1/experiences/?group=${group}&business_type=BED_ASSIGNMENT`)
  expect((await other.json()).data.find(v=>v.id===bed.id).status).toBe('pending')
  // The edited preference remains pending and cannot affect subsequent tests.
})
