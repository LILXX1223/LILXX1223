import { test, expect } from '@playwright/test'

async function openLogin(page) {
  await page.goto('/')
  await expect(page.locator('.login-page')).toBeVisible()
}

test('登录页十秒循环首尾一致，表单保持可操作', async ({ page }) => {
  await openLogin(page)
  const input = page.getByPlaceholder('请输入账号')
  await expect(input).toBeVisible()
  await expect(page.getByPlaceholder('请输入密码')).toHaveAttribute('autocomplete', 'current-password')
  const effects = await page.evaluate(() => {
    const elements = [...document.querySelectorAll('.login-atmosphere, .login-atmosphere *')]
    return { video: document.querySelectorAll('.login-page video').length,
      blocking: elements.some(el => getComputedStyle(el).pointerEvents !== 'none'),
      animations: document.querySelector('.login-page').getAnimations({subtree:true}).map(a => a.effect.getTiming().duration) }
  })
  expect(effects.video).toBe(0)
  expect(effects.blocking).toBe(false)
  expect(effects.animations.length).toBeGreaterThan(5)
  expect(effects.animations.every(duration => duration === 10000)).toBe(true)
  await page.evaluate(() => document.querySelector('.login-page').getAnimations({subtree:true}).forEach(a => {a.pause();a.currentTime=0}))
  const start = await page.screenshot()
  await page.evaluate(() => document.querySelector('.login-page').getAnimations({subtree:true}).forEach(a => {a.currentTime=10000}))
  const end = await page.screenshot()
  expect(end.equals(start)).toBe(true)
  await input.fill('test-user')
  await expect(input).toHaveValue('test-user')
})

test('桌面及窄窗口中登录卡片可读且不出现横向溢出', async ({ page }) => {
  for (const [width,height] of [[1440,900],[1024,768],[768,1024],[390,844],[320,640]]) {
    await page.setViewportSize({width,height})
    await openLogin(page)
    const info = await page.evaluate(() => {
      const card=document.querySelector('.login-card').getBoundingClientRect()
      const brand=document.querySelector('.login-brand').getBoundingClientRect()
      return {width:innerWidth,scroll:document.documentElement.scrollWidth,
        card:{left:card.left,right:card.right,width:card.width},brand:{left:brand.left,right:brand.right}}
    })
    expect(info.scroll).toBeLessThanOrEqual(info.width)
    expect(info.card.left).toBeGreaterThanOrEqual(0)
    expect(info.card.right).toBeLessThanOrEqual(width)
    expect(info.brand.left).toBeGreaterThanOrEqual(0)
    expect(info.brand.right).toBeLessThanOrEqual(width)
    await expect(page.getByRole('button',{name:'登录',exact:true})).toBeVisible()
  }
})

test('减少动态效果与低性能降级均可停用动画', async ({ browser }) => {
  const reduced = await browser.newContext({reducedMotion:'reduce'})
  const a = await reduced.newPage();await openLogin(a)
  expect(await a.evaluate(() => document.querySelector('.login-page').getAnimations({subtree:true}).length)).toBe(0)
  await reduced.close()
  const constrained = await browser.newContext()
  await constrained.addInitScript(() => Object.defineProperty(navigator,'hardwareConcurrency',{value:2,configurable:true}))
  const b = await constrained.newPage();await openLogin(b)
  await expect(b.locator('.login-atmosphere')).toHaveClass(/is-low-power/)
  expect(await b.evaluate(() => document.querySelector('.login-page').getAnimations({subtree:true}).length)).toBe(0)
  await constrained.close()
})

test('真实登录错误和授权登录仍走原接口', async ({ page }) => {
  await openLogin(page)
  await page.getByPlaceholder('请输入账号').fill('group3')
  await page.getByPlaceholder('请输入密码').fill('incorrect-test-password')
  const invalid = page.waitForResponse(r => r.url().endsWith('/api/v1/auth/login/') && r.status() === 400)
  await page.getByRole('button',{name:'登录',exact:true}).click()
  await invalid
  await expect(page.locator('.login-card')).toContainText('账号或密码错误')
  await page.getByPlaceholder('请输入密码').fill(process.env.E2E_PASSWORD)
  const valid = page.waitForResponse(r => r.url().endsWith('/api/v1/auth/login/') && r.status() === 200)
  await page.getByRole('button',{name:'登录',exact:true}).click()
  await valid
  await expect(page.getByRole('link',{name:'总览',exact:true})).toBeVisible()
  await expect(page.locator('.login-atmosphere')).toHaveCount(0)
})

test('十秒动画期间记录浏览器帧率', async ({ page }) => {
  test.setTimeout(30000)
  await openLogin(page)
  const timing = await page.evaluate(() => new Promise(resolve => {
    const frames=[];let begin
    const tick=time=>{if(begin===undefined)begin=time;frames.push(time)
      if(time-begin<10200)requestAnimationFrame(tick)
      else{const gaps=frames.slice(1).map((t,i)=>t-frames[i]).sort((a,b)=>a-b)
        resolve({frames:frames.length,duration:time-begin,p95:gaps[Math.floor(gaps.length*.95)]})}}
    requestAnimationFrame(tick)
  }))
  console.log(`登录页动画抽样：${timing.frames} 帧 / ${(timing.duration/1000).toFixed(1)} 秒，帧间隔 P95 ${timing.p95.toFixed(1)} 毫秒`)
  expect(timing.frames).toBeGreaterThan(120)
})
