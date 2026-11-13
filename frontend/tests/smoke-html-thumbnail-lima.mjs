// Read-only smoke: the standalone link is appended only to the browser's response,
// never to persisted messages or workspace files.
// LAXCODE_SMOKE_USER_ID=... LAXCODE_SMOKE_SESSION_ID=60 node tests/smoke-html-thumbnail-lima.mjs
import { chromium, expect } from '@playwright/test'
import { existsSync, mkdirSync } from 'node:fs'

const base = process.env.LAXCODE_BASE_URL || 'http://127.0.0.1:8000'
const owner = process.env.LAXCODE_SMOKE_USER_ID
const session = Number(process.env.LAXCODE_SMOKE_SESSION_ID)
const path = process.env.LAXCODE_SMOKE_HTML_PATH || 'pomodoro.html'
if (!owner || !session) throw new Error('Set LAXCODE_SMOKE_USER_ID and LAXCODE_SMOKE_SESSION_ID')
const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || (existsSync('/Applications/Microsoft Edge.app') ? 'msedge' : undefined) })
try {
  const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, acceptDownloads: true })
  await context.addCookies([
    { name: 'user_id', value: owner, url: base },
    { name: 'laxcode_preferences', value: encodeURIComponent(JSON.stringify({ owner, selected: session, global: null, models: [] })), url: base },
  ])
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(base)
  const inline = page.getByRole('button', { name: path, exact: true })
  await expect(inline).toBeVisible({ timeout: 20000 })
  expect(await page.locator('iframe').count()).toBe(0)
  await inline.click()
  await expect(page.getByRole('dialog')).toContainText('<!DOCTYPE html>')
  await page.getByRole('button', { name: '关闭文件预览' }).click()
  await page.route(`**/api/v1/sessions/${session}/messages?*`, async route => {
    const response = await route.fetch()
    const body = await response.json()
    const last = body.items.findLast(message => message.role === 'assistant')
    if (!last) throw new Error('No assistant message')
    last.content += `\n\n[HTML 缩略预览验收](workspace:/${path.split('/').map(encodeURIComponent).join('/')})`
    await route.fulfill({ response, json: body })
  })
  await page.reload()
  const card = page.getByRole('region', { name: `文件 ${path}`, exact: true })
  const iframe = card.locator('iframe')
  await expect(iframe).toBeVisible({ timeout: 20000 })
  await expect(iframe).toHaveAttribute('sandbox', '')
  await expect(iframe.contentFrame().locator('body')).toContainText('25:00')
  await expect(iframe.contentFrame().locator('script')).toHaveCount(0)
  await card.getByRole('button', { name: `查看 ${path} 源码` }).click()
  await expect(page.getByRole('dialog')).toContainText('<script>')
  await page.getByRole('button', { name: '关闭文件预览' }).click()
  await card.getByRole('button', { name: `刷新 ${path}` }).click()
  await expect(iframe).toBeVisible()
  const download = page.waitForEvent('download')
  await card.getByRole('button', { name: `下载 ${path}` }).click()
  if ((await download).suggestedFilename() !== path.split('/').at(-1)) throw new Error('Wrong download filename')
  mkdirSync('test-results', { recursive: true })
  await card.scrollIntoViewIfNeeded()
  await card.screenshot({ path: 'test-results/lima-html-thumbnail.png' })
  await page.setViewportSize({ width: 390, height: 844 })
  await card.scrollIntoViewIfNeeded()
  await card.screenshot({ path: 'test-results/lima-html-thumbnail-mobile.png' })
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('Mobile overflow')
  if (errors.length) throw new Error(errors.join('\n'))
  console.log('PASS Lima: existing inline source, standalone static HTML, refresh, source, download, mobile layout; no stored data changed')
} finally { await browser.close() }
