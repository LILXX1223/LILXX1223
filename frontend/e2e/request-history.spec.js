import { test, expect } from '@playwright/test'

test('已结束调班申请可隐藏和恢复，待审核申请不可删除',async({page})=>{
  await page.goto('/')
  await page.getByPlaceholder('请输入账号').fill('group3');await page.getByPlaceholder('请输入密码').fill(process.env.E2E_PASSWORD)
  await page.getByRole('button',{name:'登录',exact:true}).click()
  await expect(page.getByRole('link',{name:'排班管理',exact:true})).toBeVisible()
  const workspace=(await (await page.request.get('/api/v1/workspace/')).json()).data
  const csrf=decodeURIComponent((await page.context().cookies()).find(c=>c.name==='csrftoken').value),headers={'X-CSRFToken':csrf}
  async function post(path,data){const response=await page.request.post('/api/v1/'+path,{headers,data});expect(response.ok()).toBeTruthy();return (await response.json()).data}
  const initial={group:workspace.group.id,nurse:workspace.nurses[0].id,kind:'day',date:workspace.today,target_shift:'DAY'}
  const rejected=await post('requests/create/',{...initial,reason:'合成已拒绝历史申请'})
  await post(`requests/${rejected.id}/decision/`,{group:initial.group,revision:rejected.revision,decision:'rejected',note:'合成测试'})
  await post('requests/create/',{...initial,reason:'合成待审核申请'})
  await page.getByRole('link',{name:'排班管理',exact:true}).click();await page.getByRole('tab',{name:'调班申请'}).click()
  const row=page.getByRole('row').filter({hasText:'合成已拒绝历史申请'}),pending=page.getByRole('row').filter({hasText:'合成待审核申请'})
  await expect(row).toBeVisible();await expect(pending.getByRole('button',{name:'删除'})).toBeDisabled()
  await row.getByRole('button',{name:'删除'}).click()
  await page.getByRole('button',{name:'确认删除',exact:true}).click()
  await expect(row).toHaveCount(0)
  await page.getByText('已删除记录',{exact:true}).click()
  const deleted=page.getByRole('row').filter({hasText:'合成已拒绝历史申请'})
  await expect(deleted).toBeVisible();await deleted.getByRole('button',{name:'恢复'}).click()
  await page.getByRole('button',{name:'确认恢复',exact:true}).click()
  await expect(deleted).toHaveCount(0)
  await page.getByText('正常记录',{exact:true}).click()
  await expect(page.getByRole('row').filter({hasText:'合成已拒绝历史申请'})).toBeVisible()
})
