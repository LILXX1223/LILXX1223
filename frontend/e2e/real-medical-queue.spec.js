import { test, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'

test.skip(process.env.ICU_E2E_REAL_AI !== '1','仅隔离库真实 AI 联调时运行')
test('真实单床、两床和五床顺序识别，刷新恢复与单例重试',async({page})=>{
  test.setTimeout(900000)
  expect(process.env.ICU_E2E_ISOLATED).toBe('1')
  const shell=code=>execFileSync(resolve('..','.venv/bin/python'),['manage.py','shell','-c',
    "import os; assert os.environ.get('ICU_E2E_ISOLATED')=='1'; "+code],{cwd:resolve('..'),env:process.env})
  shell("from scheduling.models import CareGroup,Bed; g=CareGroup.objects.get(name='第3组'); [Bed.objects.get_or_create(group=g,number=str(n)) for n in range(36,41)]")
  await page.goto('/')
  await page.getByPlaceholder('请输入账号').fill('group3')
  await page.getByPlaceholder('请输入密码').fill(process.env.E2E_PASSWORD)
  await page.getByRole('button',{name:'登录',exact:true}).click()
  await page.getByRole('link',{name:'床位分配',exact:true}).click()
  const csrf=decodeURIComponent((await page.context().cookies()).find(c=>c.name==='csrftoken').value)
  const headers={'X-CSRFToken':csrf}
  async function post(path,data){const r=await page.request.post('/api/v1/'+path,{headers,data,timeout:240000});const body=await r.json();expect(r.ok(),`${path} HTTP ${r.status()} ${body.error?.message||''}`).toBeTruthy();return body.data}
  const group=(await (await page.request.get('/api/v1/workspace/')).json()).data.group.id
  const latest=async()=>((await (await page.request.get(`/api/v1/medical-batches/?group=${group}`)).json()).data[0])
  const waitFinished=async()=>expect.poll(async()=>{const current=await latest();return current&&current.items.every(i=>!['waiting','running'].includes(i.status))},{timeout:600000,intervals:[1000]}).toBe(true)
  async function retryTransient(exceptBed=null){
    for(let n=0;n<3;n++){
      await waitFinished();const state=await latest()
      const item=state.items.find(i=>i.status==='failed'&&i.retryable&&String(i.bed_number)!==String(exceptBed))
      if(!item)return
      await page.locator('.batch-progress .batch-item').filter({hasText:`${item.bed_number}床`}).getByRole('button',{name:'重试此床'}).click()
    }
    await waitFinished()
  }
  for(const number of [36,37]){
    const plan=await post('beds/extract-prepare/',{group,mode:'single',bed_number:number,synthetic:true,
      text:`${number}床：姓名：合成${number}；性别：男；年龄：55岁；确定诊断：肺炎；当前机械通气。`})
    const result=await post('beds/extract-prepared/',{group,token:plan.records[0].token,text:plan.records[0].text})
    expect(result.mode).toBe('external');expect(result.records[0].bed_number).toBe(number)
  }
  let running=0,maxRunning=0
  page.on('request',r=>{if(/medical-batches\/\d+\/run\//.test(r.url())){running++;maxRunning=Math.max(maxRunning,running)}})
  const end=r=>{if(/medical-batches\/\d+\/run\//.test(r.url()))running--}
  page.on('requestfinished',end);page.on('requestfailed',end)
  await page.getByText('批量识别',{exact:true}).click()
  await page.getByText('同时为病历中明确指定的空床导入合成病例',{exact:true}).click()
  const input=page.locator('.ai-box textarea')
  const caseText=n=>`${n}床：姓名：合成${n}；性别：女；年龄：56岁；确定诊断：肺炎；当前机械通气；既往CRRT已停止。`
  await input.fill([36,37].map(caseText).join('\n'))
  await page.locator('.ai-box').getByRole('button',{name:'AI识别病历',exact:true}).click()
  const progress=page.locator('.batch-progress')
  await expect(progress).toContainText('总病例 2')
  await retryTransient();await expect(progress).toContainText('成功 2',{timeout:300000})
  await expect(progress).toContainText('36床 · 成功 · 待核对')
  await expect(progress).toContainText('37床 · 成功 · 待核对')
  const two=await (await page.request.get(`/api/v1/medical-batches/?group=${group}`)).json()
  expect(two.data[0].items.every(i=>i.extraction?.mode==='external')).toBe(true)
  await input.fill([36,37,38,39,40].map(caseText).join('\n'))
  await page.locator('.ai-box').getByRole('button',{name:'AI识别病历',exact:true}).click()
  await expect(progress).toContainText('总病例 5')
  // Deliberately alter the target bed in the disposable database after preparation.
  // This causes one authentic safety-validation failure, not a simulated AI success.
  shell("from scheduling.models import CareGroup,Bed,Patient; g=CareGroup.objects.get(name='第3组'); b=Bed.objects.get(group=g,number='38'); Patient.objects.create(group=g,bed=b,name='临时合成占床',code='E2E-TEMP-38',synthetic=True)")
  await expect(progress).toContainText('成功 1',{timeout:240000})
  await page.reload()
  await expect(progress).toContainText('总病例 5')
  await retryTransient(38)
  await expect(progress).toContainText('成功 4',{timeout:600000})
  await expect(progress).toContainText('失败 1')
  await expect(progress).toContainText('38床 · 失败')
  shell("from scheduling.models import Patient; Patient.objects.filter(code='E2E-TEMP-38',synthetic=True).delete()")
  await progress.locator('.batch-item').filter({hasText:'38床'}).getByRole('button',{name:'重试此床'}).click()
  await expect(progress).toContainText('成功 5',{timeout:240000})
  await expect(progress.locator('.batch-item').filter({hasText:'38床'})).toContainText('尝试 2 次')
  await page.reload();await expect(progress).toContainText('成功 5')
  // A browser may issue the next request after the previous DB commit but
  // before the previous HTTP response finishes flushing. The safety boundary
  // is the real provider-call interval, not the browser's connection count.
  expect(maxRunning).toBeGreaterThanOrEqual(1)
  shell("from datetime import timedelta; from scheduling.models import AIRequestLog; rows=list(AIRequestLog.objects.filter(scene='medical_batch').order_by('id')); assert len([r for r in rows if r.status=='success' and r.http_status==200])==7; assert all(a.created_at+timedelta(milliseconds=a.elapsed_ms)<=b.created_at+timedelta(milliseconds=200) for a,b in zip(rows,rows[1:])), 'provider calls overlapped'")
  const list=await (await page.request.get(`/api/v1/medical-batches/?group=${group}`)).json()
  expect(list.data[0].items.every(i=>i.extraction?.mode==='external'&&!i.extraction.reviewed)).toBe(true)
  await progress.locator('.batch-item').filter({hasText:'36床'}).getByRole('button',{name:'查看核对'}).click()
  const drawer=page.getByRole('dialog',{name:'AI病历识别结果 · 待人工核对'})
  await expect(drawer).toContainText('36床')
  await drawer.locator('.el-form-item').filter({hasText:'人工核对护理负荷'}).locator('.el-select__wrapper').click()
  await page.getByRole('option',{name:'较轻'}).click()
  await drawer.getByText('已逐项核对床号、患者身份、事实及护理负荷').click()
  await drawer.getByRole('button',{name:'确认写入对应床位'}).click()
  await expect(drawer).not.toBeVisible()
  await expect(progress.locator('.batch-item').filter({hasText:'36床'})).toContainText('已核对')
})
