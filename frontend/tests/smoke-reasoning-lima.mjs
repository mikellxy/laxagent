// Real Lima model/UI smoke. Retains its disposable guest's completed conversation.
// Run from frontend/: node tests/smoke-reasoning-lima.mjs
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
async function expandAll() {
  for (const pattern of ['^(正在|已)工作$', '^(正在|已)思考$']) {
    await page.evaluate(pattern => {
      for (const button of document.querySelectorAll('button[aria-expanded="false"]')) {
        if (new RegExp(pattern).test(button.textContent)) button.click()
      }
    }, pattern)
  }
}
try {
  await page.goto(base)
  await page.getByRole('button', { name: '以游客登录' }).click()
  await expect(page.getByRole('textbox', { name: '消息输入' })).toBeVisible()
  token = decodeURIComponent((await context.cookies()).find(cookie => cookie.name === 'access_token').value)
  const project = (await json('/projects'))[0].id
  session = (await json(`/projects/${project}/sessions`))[0].id
  await page.getByRole('textbox', { name: '消息输入' }).fill('请分两个阶段整理一份 asyncio 学习资料：先创建 reasoning-smoke.txt，写入三条清晰的 Python asyncio 使用建议，并创建 reasoning-example.py 放入一个简短的并发示例；然后读取两份文件核对内容，检查建议与示例是否一致，最后简要汇报检查结果。')
  const submission = page.waitForResponse(response => response.url().endsWith(`/sessions/${session}/chat`) && response.request().method() === 'POST')
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
  runId = (await (await submission).json()).run_id
  console.log(JSON.stringify({ session_id: session, run_id: runId, progress: 'submitted' }))
  let liveReasoning = false, liveGrowth = false, previousText = '', previousStatus = ''
  const deadline = Date.now() + 180_000
  while (Date.now() < deadline) {
    await expandAll()
    const liveText = await page.evaluate(() => {
      const button = [...document.querySelectorAll('button')].find(button => button.textContent === '正在思考')
      return button ? document.getElementById(button.getAttribute('aria-controls'))?.innerText ?? null : null
    })
    if (liveText !== null) {
      liveReasoning = true
      if (previousText && liveText.startsWith(previousText) && liveText.length > previousText.length) liveGrowth = true
      previousText = liveText
    }
    const run = await json(`/runs/${runId}`)
    if (run.status !== previousStatus) console.log(JSON.stringify({ run_id: runId, progress: run.status }))
    previousStatus = run.status
    if (!['waiting', 'running'].includes(run.status)) {
      if (run.status !== 'success') throw new Error(`Run failed: ${run.error}`)
      break
    }
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  if ((await json(`/runs/${runId}`)).status !== 'success') throw new Error('Run did not finish within 180 seconds')
  await expect(page.getByRole('button', { name: '取消生成', exact: true })).toHaveCount(0)
  await expandAll()
  const history = (await json(`/sessions/${session}/messages`)).items
  const thoughts = history.filter(message => message.role === 'assistant' && message.reasoning?.length)
  if (!thoughts.length) throw new Error('Selected model produced no public reasoning')
  if (!history.some(message => message.role === 'tool')) throw new Error('No tool call completed')
  const firstTool = history.findIndex(message => message.role === 'tool')
  const preamble = history.slice(0, firstTool).filter(message => message.role === 'assistant' && message.content)
  const renderedThoughts = []
  for (const button of await page.getByRole('button', { name: '已思考', exact: true }).all()) {
    const id = await button.getAttribute('aria-controls')
    const region = page.locator(`[id="${id}"]`)
    await expect(region).toBeVisible()
    const rendered = await region.textContent()
    if (!rendered.trim()) throw new Error('Empty reasoning display')
    renderedThoughts.push(rendered)
  }
  if (renderedThoughts.length !== thoughts.length) throw new Error('Reasoning display count mismatch')
  for (const button of await page.getByRole('button', { name: '已工作', exact: true }).all()) {
    await expect(button).toHaveAttribute('aria-expanded', 'true')
  }
  mkdirSync('test-results', { recursive: true })
  await page.screenshot({ path: 'test-results/lima-reasoning-expanded.png', fullPage: true })
  const frames = (await (await request(`/runs/${runId}/events`)).text()).split('\n\n')
  const events = frames.map(frame => frame.split('\n').find(line => line.startsWith('data:'))).filter(Boolean).map(line => JSON.parse(line.slice(5)))
  for (const message of thoughts) {
    const ended = events.find(event => event.type === 'message_end' && event.msg_seq === message.seq)
    if (JSON.stringify(ended.content.reasoning) !== JSON.stringify(message.reasoning)) throw new Error('SSE/history reasoning mismatch')
  }
  await page.reload()
  await expect(page.getByRole('button', { name: '已工作', exact: true }).first()).toHaveAttribute('aria-expanded', 'false')
  await expect(page.getByRole('button', { name: '已思考', exact: true })).toHaveCount(0)
  await page.screenshot({ path: 'test-results/lima-reasoning-collapsed.png', fullPage: true })
  await expandAll()
  await expect(page.getByRole('button', { name: '已思考', exact: true })).toHaveCount(thoughts.length)
  const reloadedThoughts = []
  for (const button of await page.getByRole('button', { name: '已思考', exact: true }).all()) {
    const id = await button.getAttribute('aria-controls')
    reloadedThoughts.push(await page.locator(`[id="${id}"]`).textContent())
  }
  if (JSON.stringify(reloadedThoughts) !== JSON.stringify(renderedThoughts)) throw new Error('Reasoning changed after reload')
  if (errors.length) throw new Error(errors.join('; '))
  console.log(JSON.stringify({ status: 'PASS', session_id: session, run_id: runId, reasoning_messages: thoughts.length,
    reasoning_chunks: events.filter(event => event.type === 'reasoning_chunk').length,
    live_reasoning_observed: liveReasoning, live_text_growth_observed: liveGrowth,
    operation_preambles: preamble.map(message => message.content),
    checks: 'real model, tools, streamed nested disclosures, completed history, SSE snapshot, reload defaults' }))
} catch (error) {
  mkdirSync('test-results', { recursive: true })
  await page.screenshot({ path: 'test-results/lima-reasoning-failure.png', fullPage: true }).catch(() => {})
  if (runId) await request(`/runs/${runId}/cancel`, 'POST').catch(() => {})
  throw error
} finally { await browser.close() }
