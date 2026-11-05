import { test, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'

test.skip(process.env.ICU_E2E_REAL_AI !== '1','仅 --real-ai 隔离联调时调用真实服务')
test('真实模型按床识别长合成病例，人工确认后才保存',async({page})=>{
  test.setTimeout(300000)
  // This fixture can only run against the disposable database created by the
  // browser test runner. It never modifies the project's business database.
  expect(process.env.ICU_E2E_ISOLATED).toBe('1')
  execFileSync(resolve('..','.venv/bin/python'),['manage.py','shell','-c',
    "import os; assert os.environ.get('ICU_E2E_ISOLATED')=='1'; from scheduling.models import CareGroup,Bed,Patient; g=CareGroup.objects.get(name='第3组'); Patient.objects.filter(group=g,synthetic=True,bed__number__gt=2).update(bed=None); [Bed.objects.get_or_create(group=g,number=i) for i in range(1,54)]"],{cwd:resolve('..'),env:process.env})
  await page.goto('/')
  await page.getByPlaceholder('请输入账号').fill('group3')
  await page.getByPlaceholder('请输入密码').fill(process.env.E2E_PASSWORD)
  await page.getByRole('button',{name:'登录',exact:true}).click()
  await expect(page.getByRole('link',{name:'床位分配',exact:true})).toBeVisible()
  const csrf=decodeURIComponent((await page.context().cookies()).find(c=>c.name==='csrftoken').value)
  const headers={'X-CSRFToken':csrf}
  const get=async path=>(await (await page.request.get('/api/v1/'+path)).json()).data
  async function post(path,data){
    const response=await page.request.post('/api/v1/'+path,{headers,data,timeout:150000})
    const body=await response.json()
    expect(response.ok(),`${path}: ${response.status()} ${body.error?.message||''}`).toBeTruthy()
    return body.data
  }
  const before=await get('workspace/'),group=before.group.id
  expect(before.board.beds).toHaveLength(53)
  expect(before.board.beds.filter(b=>b.patient)).toHaveLength(2)
  const single=await post('beds/extract-prepare/',{group,mode:'single',bed_number:3,synthetic:true,
    text:'姓名：合成丙；性别：男；年龄：48岁；确定诊断：肺炎；目前机械通气。'})
  const singleResult=await post('beds/extract-prepared/',{group,token:single.records[0].token,text:single.records[0].text})
  expect(singleResult.mode).toBe('external');expect(singleResult.records[0].bed_number).toBe(3)
  const filler='合成病程记录：患者生命体征平稳，护理措施继续按计划执行。'.repeat(160)
  const emptyBeds=Array.from({length:51},(_,i)=>`${i+3}床：姓名：合成空床测试；诊断：合成测试。`).join('\n')
  const text=`1床：住院号：SYN-101；姓名：合成甲；性别：男；年龄：55岁；确定诊断：肺炎；当前机械通气。${filler}\n1床：住院号：SYN-101；复查：目前仍机械通气。\n2床：住院号：SYN-102；姓名：合成乙；性别：女；年龄：60岁；确定诊断：感染；既往CRRT已停。${filler}\n${emptyBeds}`
  expect(text.length).toBeGreaterThan(9000)
  const plan=await post('beds/extract-prepare/',{group,mode:'batch',text,synthetic:true})
  expect(plan.records).toHaveLength(2);expect(plan.skipped).toHaveLength(51);expect(plan.ai_parameters.thinking).toBe('disabled')
  const outputs=[]
  for(const item of plan.records){
    const result=await post('beds/extract-prepared/',{group,token:item.token,text:item.text})
    expect(result.mode).toBe('external');expect(result.records[0].bed_number).toBe(item.bed_number)
    for(const key of ['diagnoses','suspected','treatments','historical','suggestions','evidence'])expect(Array.isArray(result.records[0].fields[key])).toBe(true)
    outputs.push(result)
  }
  const untouched=await get('workspace/')
  expect(untouched.board.beds.map(b=>b.patient)).toEqual(before.board.beds.map(b=>b.patient))
  const items=outputs.map(output=>({extraction:output.id,records:output.records.map(record=>({...record,fields:{...record.fields,load:'light'}}))}))
  const confirmed=await post('beds/review-batch/',{group,items,confirmed:true})
  expect(confirmed.patients).toHaveLength(2)
  const duplicate=await page.request.post('/api/v1/beds/review-batch/',{headers,data:{group,items,confirmed:true}})
  expect(duplicate.status()).toBe(409)
})
