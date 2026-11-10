// Real model and browser acceptance using a disposable guest account.
// Run from frontend/: node tests/smoke-memory-lima.mjs
import { chromium, expect } from '@playwright/test'
import { existsSync, mkdirSync } from 'node:fs'

const base = process.env.LAXCODE_BASE_URL || 'http://127.0.0.1:8000'
const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || (existsSync('/Applications/Microsoft Edge.app') ? 'msedge' : undefined) })
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
let token, session, runId
async function request(path, method = 'GET', data) {
  const response = await context.request.fetch(`${base}/api/v1${path}`, { method, data, headers: { Authorization: `Bearer ${token}` } })
  if (!response.ok()) throw new Error(`${method} ${path}: ${response.status()}`)
  return response
}
async function json(path) { return (await request(path)).json() }
async function send(prompt) {
  await page.getByRole('textbox', { name: '消息输入' }).fill(prompt)
  const submission = page.waitForResponse(response => response.url().endsWith(`/sessions/${session}/chat`) && response.request().method() === 'POST')
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
  runId = (await (await submission).json()).run_id
  console.log(JSON.stringify({ run_id: runId, progress: 'submitted' }))
  const deadline = Date.now() + 180_000
  while (Date.now() < deadline) {
    const run = await json(`/runs/${runId}`)
    if (!['waiting', 'running'].includes(run.status)) {
      if (run.status !== 'success') throw new Error(`Run ${runId} ended ${run.status}: ${run.error}`)
      await expect(page.getByRole('button', { name: '取消生成', exact: true })).toHaveCount(0)
      return runId
    }
    await new Promise(resolve => setTimeout(resolve, 500))
  }
  throw new Error(`Run ${runId} timed out`)
}
async function allMemories() { return (await json('/memories')).categories.flatMap(group => group.items) }

try {
  await page.goto(base)
  await page.getByRole('button', { name: '以游客登录' }).click()
  await expect(page.getByRole('textbox', { name: '消息输入' })).toBeVisible()
  token = decodeURIComponent((await context.cookies()).find(cookie => cookie.name === 'access_token').value)
  const project = (await json('/projects'))[0].id
  session = (await json(`/projects/${project}/sessions`))[0].id
  const toggle = page.getByRole('switch', { name: '记忆', exact: true })
  await expect(toggle).toBeEnabled()
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  if (Object.keys((await json('/user-configs')).configs).length) throw new Error('Guest configurations should be empty')
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  const enabledRun = await send('请记住以下两个独立的长期偏好，分别调用 write_memory 保存为两条 preference 记忆：我喜欢用中文讨论技术；我喜欢在代码示例中优先使用 Python。请保持两条独立，不要合并。完成后简短回复。')
  const saved = await allMemories()
  if (saved.length < 2) throw new Error(`Expected two saved memories; got ${saved.length}`)
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  const historyBefore = (await json(`/sessions/${session}/messages`)).items
  const disabledRun = await send('请把我的另一个长期偏好记住：讲解时先给结论。如果当前没有记忆工具，请直接说明未保存，不要调用其他工具替代。')
  if ((await allMemories()).length !== saved.length) throw new Error('Disabled run wrote new memory')
  const history = (await json(`/sessions/${session}/messages`)).items
  const newMessages = history.filter(message => message.seq > historyBefore.at(-1).seq)
  if (newMessages.some(message => message.role === 'tool' && ['write_memory', 'list_memory_abstracts', 'get_memory_details'].includes(message.tool_name))) throw new Error('Disabled run attempted memory tool')
  await page.reload()
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await page.getByRole('button', { name: '查看记忆', exact: true }).click()
  const drawer = page.getByRole('dialog', { name: '用户记忆', exact: true })
  await expect(drawer.getByRole('region')).toHaveCount(6)
  await expect(drawer.getByRole('button', { name: `记忆 ${saved[0].id} 详情`, exact: true })).toBeVisible()
  mkdirSync('test-results', { recursive: true })
  await page.screenshot({ path: 'test-results/lima-memory-desktop.png', fullPage: true })
  await drawer.getByRole('button', { name: `记忆 ${saved[0].id} 详情`, exact: true }).hover()
  await expect(page.getByRole('tooltip').filter({ hasText: saved[0].detail })).toBeVisible()
  await drawer.getByRole('button', { name: `删除记忆 ${saved[0].id}`, exact: true }).click()
  await page.getByRole('dialog', { name: `删除记忆 ${saved[0].id}？`, exact: true }).getByRole('button', { name: '确认删除', exact: true }).click()
  await expect(drawer.getByRole('button', { name: `删除记忆 ${saved[0].id}`, exact: true })).toHaveCount(0)
  if ((await allMemories()).length !== saved.length - 1) throw new Error('Single deletion failed')
  await page.setViewportSize({ width: 390, height: 844 })
  if ((await drawer.boundingBox()).width !== 390) throw new Error('Narrow drawer does not fit viewport')
  await page.screenshot({ path: 'test-results/lima-memory-mobile.png', fullPage: true })
  await drawer.getByRole('button', { name: '全部删除', exact: true }).click()
  await page.getByRole('dialog', { name: '删除全部记忆？', exact: true }).getByRole('button', { name: '确认删除', exact: true }).click()
  await expect(drawer.getByText('0 条记忆', { exact: true })).toBeVisible()
  if ((await allMemories()).length) throw new Error('Delete all failed')
  if (JSON.stringify((await json(`/sessions/${session}/messages`)).items) !== JSON.stringify(history)) throw new Error('Deletion modified conversation history')
  if (errors.length) throw new Error(errors.join('; '))
  console.log(JSON.stringify({ status: 'PASS', session_id: session, enabled_run: enabledRun, disabled_run: disabledRun,
    checks: 'default off, real memory write, disable next run, persisted config, grouped drawer, full text, single/all delete, history preservation, desktop/mobile layout' }))
} catch (error) {
  mkdirSync('test-results', { recursive: true })
  await page.screenshot({ path: 'test-results/lima-memory-failure.png', fullPage: true }).catch(() => {})
  if (runId) await request(`/runs/${runId}/cancel`, 'POST').catch(() => {})
  throw error
} finally { await browser.close() }
