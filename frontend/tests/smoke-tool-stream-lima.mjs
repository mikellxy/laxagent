// Real Lima service/model: early tool cards, growing arguments, reconciliation and cancellation.
// Run from frontend/: node tests/smoke-tool-stream-lima.mjs
import { chromium, expect } from '@playwright/test'
import { existsSync, mkdirSync } from 'node:fs'

const base = process.env.LAXCODE_BASE_URL || 'http://127.0.0.1:8000'
const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || (existsSync('/Applications/Microsoft Edge.app') ? 'msedge' : undefined) })
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
let token, session, runId
async function request(path, method = 'GET') {
  const response = await context.request.fetch(`${base}/api/v1${path}`, { method, headers: { Authorization: `Bearer ${token}` } })
  if (!response.ok()) throw new Error(`${method} ${path}: ${response.status()}`)
  return response
}
async function json(path) { return (await request(path)).json() }
async function expandWork() {
  await page.evaluate(() => {
    for (const button of document.querySelectorAll('button[aria-expanded="false"]')) {
      if (/^(正在|已)工作$/.test(button.getAttribute('aria-label'))) button.click()
    }
  })
}
async function submit(query) {
  await page.getByRole('textbox', { name: '消息输入' }).fill(query)
  const submission = page.waitForResponse(r => r.url().endsWith(`/sessions/${session}/chat`) && r.request().method() === 'POST')
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
  runId = (await (await submission).json()).run_id
  console.log(JSON.stringify({ session_id: session, run_id: runId, progress: 'submitted' }))
}
try {
  await page.goto(base)
  await page.getByRole('button', { name: '以游客登录' }).click()
  await expect(page.getByRole('textbox', { name: '消息输入' })).toBeVisible()
  token = decodeURIComponent((await context.cookies()).find(c => c.name === 'access_token').value)
  const project = (await json('/projects'))[0].id
  session = (await json(`/projects/${project}/sessions`))[0].id
  await submit('这是工具流式显示验收。请先用一句话说明，然后用 write_file 分别创建 tool-stream-a.md 和 tool-stream-b.md，每个文件直接写入约800字的中文 asyncio 学习建议。必须直接生成文件全文作为 content 参数，不用脚本或 bash，不调用其他工具，完成后简短回复。')
  const lengths = new Map()
  let observed = false, growth = false, screenshot = false
  const deadline = Date.now() + 180_000
  while (Date.now() < deadline) {
    await expandWork()
    for (const card of await page.getByRole('button', { name: 'write_file · 正在生成参数', exact: true }).all()) {
      observed = true
      if (await card.getAttribute('aria-expanded') === 'false') await card.click()
      const id = await card.getAttribute('aria-controls')
      const text = await page.locator(`[id="${id}"]`).textContent()
      if ((lengths.get(id) ?? 0) > 0 && text.length > lengths.get(id)) growth = true
      lengths.set(id, text.length)
      if (text.length > 100 && !screenshot) {
        mkdirSync('test-results', { recursive: true })
        await page.screenshot({ path: 'test-results/lima-tool-generating.png' })
        screenshot = true
      }
    }
    const run = await json(`/runs/${runId}`)
    if (!['waiting', 'running'].includes(run.status)) {
      if (run.status !== 'success') throw new Error(`Run ended: ${run.status}, ${run.error_code}`)
      break
    }
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  if (!observed || !growth) throw new Error('Did not observe a tool card growing before execution')
  await expect(page.getByRole('button', { name: '取消生成', exact: true })).toHaveCount(0, { timeout: 15_000 })
  await expandWork()
  const history = (await json(`/sessions/${session}/messages`)).items
  const tools = history.filter(m => m.role === 'tool')
  if (tools.length !== 2 || tools.some(m => m.tool_name !== 'write_file')) throw new Error('Expected two write_file calls')
  await expect(page.getByLabel('工具调用', { exact: true })).toHaveCount(2)
  const frames = (await (await request(`/runs/${runId}/events`)).text()).split('\n\n')
  const events = frames.map(frame => frame.split('\n').find(line => line.startsWith('data:'))).filter(Boolean).map(line => JSON.parse(line.slice(5)))
  for (const event of events.filter(e => e.type === 'tool_start')) {
    const id = event.content.tool_call_id
    const start = events.findIndex(e => e.type === 'tool_call_start' && e.content.tool_call_id === id)
    const delta = events.findIndex(e => e.type === 'tool_call_delta' && e.content.tool_call_id === id)
    const execute = events.indexOf(event)
    if (!(start >= 0 && start < delta && delta < execute)) throw new Error('Invalid generation/execution order')
    if (events[start].run_id !== runId || events[start].resume_time !== 0) throw new Error('Missing full call identity')
    const argumentsText = events.filter(e => e.type === 'tool_call_delta' && e.content.tool_call_id === id).map(e => e.content.arguments_delta).join('')
    if (argumentsText !== event.content.arguments) throw new Error('Streamed arguments differ from execution')
  }
  const completedRun = runId
  await submit('取消操作验收：请直接调用 write_file 创建 tool-stream-cancel.md，content 写一篇5000字的中文异步编程教程，不用其他工具。')
  await expect(async () => {
    await expandWork()
    await expect(page.getByRole('button', { name: 'write_file · 正在生成参数', exact: true })).toBeVisible()
  }).toPass({ timeout: 120_000, intervals: [100] })
  await page.getByRole('button', { name: '取消生成', exact: true }).click()
  await expect(page.getByRole('button', { name: 'write_file · 已中断', exact: true })).toBeVisible({ timeout: 20_000 })
  if ((await json(`/runs/${runId}`)).status !== 'cancelled') throw new Error('Cancellation did not finish')
  await page.screenshot({ path: 'test-results/lima-tool-interrupted.png' })
  await page.reload()
  await expect(page.getByRole('button', { name: '继续', exact: true })).toBeVisible()
  await expandWork()
  await expect(page.getByLabel('工具调用', { exact: true })).toHaveCount(2)
  if (errors.length) throw new Error(errors.join('; '))
  console.log(JSON.stringify({ status: 'PASS', session_id: session, completed_run: completedRun, cancelled_run: runId,
    early_card: observed, incremental_arguments: growth, tool_calls: tools.length,
    deltas: events.filter(e => e.type === 'tool_call_delta').length }))
} catch (error) {
  mkdirSync('test-results', { recursive: true })
  await page.screenshot({ path: 'test-results/lima-tool-stream-failure.png', fullPage: true }).catch(() => {})
  if (runId) await request(`/runs/${runId}/cancel`, 'POST').catch(() => {})
  throw error
} finally { await browser.close() }
