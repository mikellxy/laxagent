// Real Lima workspace reads, fresh guest fixtures; reply links injected only in the browser.
// Uses the service started with config/config.json; no LLM calls or stored messages.
import { chromium, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync } from 'node:fs'
const base = process.env.LAXAGENT_BASE_URL || 'http://127.0.0.1:8000'
const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || (existsSync('/Applications/Microsoft Edge.app') ? 'msedge' : undefined) })
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 950 }, acceptDownloads: true })
  const errors = []; page.on('pageerror', error => errors.push(error.message))
  await page.goto(base)
  const loginResponse = page.waitForResponse(response => response.url().endsWith('/api/v1/login'))
  await page.getByRole('button', { name: '以游客登录' }).click()
  const login = await (await loginResponse).json()
  await expect(page.getByRole('textbox', { name: '消息输入' })).toBeVisible()
  const owner = (await page.context().cookies()).find(cookie => cookie.name === 'user_id').value
  const headers = { Authorization: `Bearer ${login.access_token}` }
  const session = await (await page.request.get(`${base}/api/v1/sessions/${login.initial_session_id}`, { headers })).json()
  const script = `import json, sys\nfrom PIL import Image, ImageDraw\nfrom laxagent.layout import get_project_workdir\nowner, project = json.loads(sys.argv[1])\np = get_project_workdir(owner, project)\np.mkdir(parents=True, exist_ok=True)\nimage = Image.new('RGB', (480, 300), '#b7d7e2')\ndraw = ImageDraw.Draw(image)\ndraw.ellipse((320, 40, 380, 100), fill='#fff0bd')\ndraw.polygon([(0,300),(160,150),(300,300)], fill='#548a9f')\nimage.save(p/'image.png')\nimage.save(p/'second.png')\n(p/'page.html').write_text('<!doctype html><style>body{background:#eaf3ef;color:#294d42;padding:48px;font-family:sans-serif}h1{font-size:52px}</style><h1>A quiet workspace</h1><p>Build something useful.</p><script>alert(1)</script>')\n(p/'style.css').write_text('.chat-input {\\n  min-height: 48px;\\n  max-height: 180px;\\n  resize: none;\\n  overflow-y: auto;\\n  white-space: pre-wrap;\\n  line-height: 1.5;\\n}\\n')\n`
  execFileSync('limactl', ['shell', process.env.VM_NAME || 'laxagent', '--', 'bash', '-lc', 'cd ~/code/laxagent && .venv/bin/python -c "$1" "$2"', 'preview-smoke', script, JSON.stringify([decodeURIComponent(owner), session.project_id])], { timeout: 30000 })
  await page.route(`**/sessions/${session.id}/messages?*`, async route => {
    const response = await route.fetch(), body = await response.json()
    body.items = [{ seq: 1, role: 'assistant', content: '图片逐张展示。\n\n[图片](workspace:/image.png)\n\n[图片二](workspace:/second.png)\n\n[网页](workspace:/page.html)\n\nCSS 参考\n\n[代码](workspace:/style.css)' }]
    await route.fulfill({ response, json: body })
  })
  await page.reload()
  const card = path => page.getByRole('region', { name: `文件 ${path}`, exact: true })
  await expect(card('image.png').getByRole('img')).toBeVisible({ timeout: 20000 })
  for (const path of ['image.png', 'page.html']) await expect(card(path).locator('header')).toHaveCount(0)
  const first = await card('image.png').boundingBox(), second = await card('second.png').boundingBox()
  expect(second.y).toBeGreaterThanOrEqual(first.y + first.height)
  mkdirSync('test-results', { recursive: true })
  await card('image.png').screenshot({ path: 'test-results/lima-bare-image.png' })
  await card('page.html').screenshot({ path: 'test-results/lima-bare-html.png' })
  await card('style.css').screenshot({ path: 'test-results/lima-terminal-card.png' })
  await card('page.html').getByRole('button', { name: '查看 page.html 源码' }).click()
  await expect(page.getByRole('dialog')).toContainText('<script>')
  await page.getByRole('button', { name: '关闭文件预览' }).click()
  await card('image.png').getByRole('button', { name: '查看 image.png' }).click()
  await expect(page.getByRole('dialog').getByRole('img')).toBeVisible()
  await page.getByRole('button', { name: '关闭文件预览' }).click()
  const download = page.waitForEvent('download')
  await card('image.png').getByRole('button', { name: '下载 image.png' }).click()
  expect((await download).suggestedFilename()).toBe('image.png')
  await page.setViewportSize({ width: 390, height: 844 })
  await card('style.css').getByRole('button', { name: '展开 style.css' }).click()
  await expect(page.getByRole('dialog')).toContainText('min-height: 48px')
  await expect(page.getByRole('button', { name: '关闭文件预览' })).toHaveCSS('width', '30px')
  await page.getByRole('dialog').screenshot({ path: 'test-results/lima-terminal-mobile.png' })
  await page.keyboard.press('Escape')
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('Mobile overflow')
  if (errors.length) throw new Error(errors.join('\n'))
  console.log('PASS Lima: real workspace images/HTML/text, stacked media, source/image dialogs, compact mobile toolbar and download; no LLM calls')
} finally { await browser.close() }
