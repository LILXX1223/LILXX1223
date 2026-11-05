import { defineConfig } from '@playwright/test'

if (process.env.ICU_E2E_ISOLATED !== '1' || !process.env.E2E_BASE_URL) {
  throw new Error('浏览器测试必须通过项目根目录的 python scripts/run_browser_tests.py 在隔离环境运行。')
}

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 90000,
  expect: { timeout: 10000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: process.env.E2E_BASE_URL,
    viewport: { width: 1440, height: 1000 },
    timezoneId: 'Asia/Shanghai',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
})
