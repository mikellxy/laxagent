// Explicit integration smoke: fresh browser identity, real Lima service and LLM.
// Run with: node tests/smoke-lima.mjs (from frontend/). Retains DB records and workspace files.
import { chromium, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { mkdirSync, existsSync } from 'node:fs'

const vm = process.env.VM_NAME || 'laxcode'
const base = process.env.LAXCODE_BASE_URL || 'http://127.0.0.1:8000'
const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || (existsSync('/Applications/Microsoft Edge.app') ? 'msedge' : undefined) })
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
const eventRequests = []
page.on('request', request => { if (request.url().includes('/events')) eventRequests.push(request.url()) })
const runIds = []
let owner, session, project
async function request(path) {
  const cookies = await context.cookies()
  const token = decodeURIComponent(cookies.find(c => c.name === 'access_token').value)
  const response = await context.request.get(base + '/api/v1' + path, { headers: { Authorization: `Bearer ${token}` } })
  if (!response.ok()) throw new Error(`API returned ${response.status()} for ${path}`)
  return response.json()
}
async function waitRun(id) {
  await expect.poll(async () => (await request(`/runs/${id}`)).status, { timeout: 400_000, intervals: [300, 500, 1000] }).not.toMatch(/^(waiting|running)$/)
  const result = await request(`/runs/${id}`)
  if (result.status !== 'success') throw new Error(`Run ${id}: ${result.error}`)
  console.log(JSON.stringify({ run_id: id, status: result.status }))
}
async function send(query) {
  await page.getByRole('textbox', { name: '消息输入' }).fill(query)
  await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeEnabled()
  const response = page.waitForResponse(r => r.url().endsWith(`/sessions/${session}/chat`) && r.request().method() === 'POST')
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
  const submitted = await response
  if (submitted.status() !== 202) throw new Error(`Chat returned ${submitted.status()}`)
  const body = await submitted.json(); runIds.push(body.run_id)
  return body.run_id
}
function inVM(script) {
  return execFileSync('limactl', ['shell', vm, '--', 'bash', '-lc', 'cd ~/code/laxcode && .venv/bin/python -c "$1"', 'smoke', script], { encoding: 'utf8', timeout: 120_000 })
}
try {
  await page.goto(base)
  await page.getByRole('button', { name: '以游客登录' }).click()
  await expect(page.getByRole('button', { name: 'laxcode', exact: true })).toHaveAttribute('aria-current', 'page')
  await expect(page.getByRole('textbox', { name: '消息输入' })).toBeVisible()
  owner = decodeURIComponent((await context.cookies()).find(c => c.name === 'user_id').value)
  const initial = (await request('/projects')).find(p => p.name === 'LaxCode')
  if (!initial) throw new Error('Guest project missing')
  await page.getByRole('button', { name: '新建', exact: true }).click()
  await page.getByRole('textbox', { name: '名称' }).fill('Web 验收')
  await page.getByRole('button', { name: '确定', exact: true }).click()
  const group = page.getByRole('region', { name: 'Web 验收', exact: true })
  const small = group.getByRole('button', { name: '新建会话', exact: true })
  await expect(small).toBeVisible()
  if ((await small.boundingBox()).width >= (await group.boundingBox()).width) throw new Error('Empty action is not inset')
  await small.click(); await expect(small).toHaveCount(0)
  await expect(page.getByRole('button', { name: '会话 1', exact: true })).toHaveAttribute('aria-current', 'page')
  project = (await request('/projects')).find(p => p.name === 'Web 验收').id
  session = (await request(`/projects/${project}/sessions`))[0].id
  await page.getByRole('button', { name: '修改会话名 会话 1', exact: true }).click()
  await page.getByRole('textbox', { name: '名称' }).fill('Lima UI 验收')
  await page.getByRole('button', { name: '确定', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Lima UI 验收', exact: true })).toHaveAttribute('aria-current', 'page')
  const query = '请逐行写出 12 条 Python asyncio 学习笔记，每条一句，使用 Markdown 编号列表，不调用工具。'
  const firstRun = await send(query)
  const beforeReload = (await request(`/runs/${firstRun}`)).status
  await page.reload()
  await expect(page.getByRole('button', { name: 'Lima UI 验收', exact: true })).toHaveAttribute('aria-current', 'page')
  await waitRun(firstRun)
  await expect(page.getByLabel('助手消息', { exact: true })).toBeVisible({ timeout: 20_000 })
  await expect(page.getByLabel('用户消息', { exact: true })).toHaveCount(1)
  if (!eventRequests.some(url => url.endsWith(`/runs/${firstRun}/events`))) throw new Error('No SSE subscription')
  console.log(JSON.stringify({ before_reload: beforeReload, sse_connections: eventRequests.length, user_id: owner, project_id: project, session_id: session }))
  const marker = `WEB-TOOL-${Date.now()}`
  inVM(`from pathlib import Path\nfrom laxagent.layout import get_project_workdir\np = get_project_workdir(${JSON.stringify(owner)}, ${project}) / 'hello.txt'\nassert p.parent.is_dir()\np.write_text(${JSON.stringify(marker + '\n')}, encoding='utf-8')`)
  const secondRun = await send('请调用 read_file 读取 hello.txt 的第一行，然后原样回复文件内容。')
  await waitRun(secondRun)
  await expect(page.getByLabel('工具调用', { exact: true }).first()).toContainText('read_file', { timeout: 20_000 })
  await expect(page.getByLabel('工具调用', { exact: true }).first()).toContainText('hello.txt')
  await expect(page.getByLabel('工具调用', { exact: true }).first()).not.toContainText(marker)
  await expect(page.getByLabel('助手消息', { exact: true }).last()).toContainText(marker, { timeout: 20_000 })
  await expect(page.getByLabel('用户消息', { exact: true })).toHaveCount(2)
  mkdirSync('test-results', { recursive: true })
  await page.screenshot({ path: 'test-results/lima-chat.png', fullPage: true })
  // A separate session checks real SQL cursor pagination without extra LLM calls.
  await group.getByRole('button', { name: 'Web 验收 新会话', exact: true }).click()
  await expect(page.getByRole('button', { name: '会话 2', exact: true })).toHaveAttribute('aria-current', 'page')
  const paginationSession = (await request(`/projects/${project}/sessions`)).at(-1).id
  inVM(`import asyncio\nfrom pathlib import Path\nfrom laxagent.config import load_config\nfrom laxagent.storage.database import Database\nfrom laxagent.storage.repositories import Repository\nasync def seed():\n    cfg = load_config(Path('config/config.json'))\n    async with Database.open(cfg.database) as db:\n        repo = Repository(db)\n        for n in range(120):\n            await repo.append_message(user_id=${JSON.stringify(owner)}, project_id=${project}, session_id=${paginationSession}, role='user' if n % 2 == 0 else 'assistant', content=f'分页验收第 {n + 1} 条\\n保留阅读位置。')\nasyncio.run(seed())`)
  await page.reload()
  await expect(page.locator('[data-seq="120"]')).toBeVisible({ timeout: 20_000 })
  const list = page.getByLabel('消息列表', { exact: true })
  await list.evaluate(el => { el.scrollTop = 300 })
  await expect(page.getByRole('button', { name: '回到最新' })).toBeVisible()
  await list.evaluate(el => { el.scrollTop = 80 })
  await expect(page.locator('[data-seq="21"]')).toBeAttached({ timeout: 20_000 })
  await page.getByRole('button', { name: '回到最新' }).click()
  await expect(page.locator('[data-seq="120"]')).toBeVisible()
  await page.screenshot({ path: 'test-results/lima-pagination.png', fullPage: true })
  if (errors.length) throw new Error(errors.join('; '))
  console.log(JSON.stringify({ status: 'PASS', user_id: owner, project_id: project, chat_session_id: session, pagination_session_id: paginationSession, run_ids: runIds, checks: 'guest, project, session, rename, reload, SSE, real tool, SQL pagination, scrolling', screenshots: ['lima-chat.png', 'lima-pagination.png'] }))
} catch (error) {
  mkdirSync('test-results', { recursive: true })
  await page.screenshot({ path: 'test-results/lima-failure.png', fullPage: true })
  console.error(JSON.stringify({ owner, project, session, errors, body: await page.locator('body').innerText() }))
  throw error
} finally {
  await browser.close()
}
