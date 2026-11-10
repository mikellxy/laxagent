// Real Lima service/LLM acceptance. Creates a disposable guest and soft-deletes its session.
// Run from frontend/: node tests/smoke-control-lima.mjs
import { chromium, expect } from '@playwright/test'
import { existsSync, mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'

const base = process.env.LAXCODE_BASE_URL || 'http://127.0.0.1:8000'
const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || (existsSync('/Applications/Microsoft Edge.app') ? 'msedge' : undefined) })
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
let owner, session, project
const runs = []
async function request(path, method = 'GET') {
  const token = decodeURIComponent((await context.cookies()).find(c => c.name === 'access_token').value)
  return context.request.fetch(base + '/api/v1' + path, { method, headers: { Authorization: `Bearer ${token}` } })
}
async function json(path) {
  const response = await request(path)
  if (!response.ok()) throw new Error(`${path}: ${response.status()}`)
  return response.json()
}
async function send(content) {
  await page.getByRole('textbox', { name: '消息输入' }).fill(content)
  const response = page.waitForResponse(r => r.url().endsWith(`/sessions/${session}/chat`) && r.request().method() === 'POST')
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
  const submitted = await response
  if (submitted.status() !== 202) throw new Error(`chat: ${submitted.status()}`)
  const id = (await submitted.json()).run_id
  runs.push(id)
  return id
}
async function modelStarted(id) {
  const token = decodeURIComponent((await context.cookies()).find(c => c.name === 'access_token').value)
  const abort = new AbortController()
  const timeout = setTimeout(() => abort.abort(), 60_000)
  try {
    const response = await fetch(`${base}/api/v1/runs/${id}/events`, { headers: { Authorization: `Bearer ${token}` }, signal: abort.signal })
    if (!response.ok) throw new Error(`SSE: ${response.status}`)
    const reader = response.body.getReader(), decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const chunk = await reader.read()
      if (chunk.done) throw new Error('Run ended before a model event')
      buffer += decoder.decode(chunk.value, { stream: true })
      let boundary
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2)
        const data = frame.split('\n').find(line => line.startsWith('data:'))
        if (data && JSON.parse(data.slice(5)).msg_seq !== null) {
          console.log(JSON.stringify({ run_id: id, progress: 'model stream started' }))
          return
        }
      }
    }
  } finally { clearTimeout(timeout); abort.abort() }
}
async function cancelled(id) {
  const run = await json(`/runs/${id}`)
  if (run.status !== 'cancelled' || run.error !== null || !run.finished_at) throw new Error('Cancellation was not persisted')
  const events = await (await request(`/runs/${id}/events`)).text()
  if ((events.match(/event: sse_end/g) ?? []).length !== 1 || !events.includes('"status": "cancelled"')) throw new Error('Missing or duplicated cancellation event')
}
try {
  await page.goto(base)
  await page.getByRole('button', { name: '以游客登录' }).click()
  await expect(page.getByRole('textbox', { name: '消息输入' })).toBeVisible()
  owner = decodeURIComponent((await context.cookies()).find(c => c.name === 'user_id').value)
  const projects = await json('/projects')
  project = projects[0].id
  const sessions = await json(`/projects/${project}/sessions`)
  session = sessions[0].id
  const name = sessions[0].name
  const longPrompt = '直接连续输出从 1 到 10000 的所有整数，每行一个数字。不要解释，不要调用工具。'
  const first = await send(longPrompt)
  await modelStarted(first)
  await page.getByRole('button', { name: '取消生成', exact: true }).click()
  await expect(page.getByRole('status')).toHaveText('已取消', { timeout: 30_000 })
  await cancelled(first)
  console.log(JSON.stringify({ run_id: first, progress: 'cancelled and SSE retained' }))
  await page.reload()
  await expect(page.getByRole('status')).toHaveText('已取消')
  const history = await json(`/sessions/${session}/messages`)
  if (history.items.some(m => m.role === 'assistant')) throw new Error('Unfinished answer was persisted')
  const second = await send('只回复 CONTINUE_OK，不要调用工具。')
  await expect.poll(async () => (await json(`/runs/${second}`)).status, { timeout: 120_000, intervals: [300, 500, 1000] }).not.toMatch(/^(waiting|running)$/)
  if ((await json(`/runs/${second}`)).status !== 'success') throw new Error('Conversation did not continue successfully')
  await expect(page.getByLabel('助手消息', { exact: true }).last()).toContainText('CONTINUE_OK', { timeout: 20_000 })
  console.log(JSON.stringify({ run_id: second, progress: 'continued successfully' }))
  const third = await send(longPrompt)
  await modelStarted(third)
  await page.getByRole('button', { name: `删除会话 ${name}`, exact: true }).click()
  mkdirSync('test-results', { recursive: true })
  await page.screenshot({ path: 'test-results/lima-delete-confirm.png', fullPage: true })
  await page.getByRole('dialog').getByRole('button', { name: '删除', exact: true }).click()
  await expect(page.getByLabel('聊天', { exact: true })).toHaveCount(0, { timeout: 30_000 })
  if ((await request(`/sessions/${session}`)).status() !== 404) throw new Error('Deleted session accessible')
  if ((await request(`/runs/${third}/events`)).status() !== 404) throw new Error('Deleted session stream accessible')
  if ((await request(`/sessions/${session}`, 'DELETE')).status() !== 204) throw new Error('Delete not idempotent')
  await page.reload()
  await expect(page.getByRole('button', { name: '新建会话', exact: true })).toBeVisible()
  await expect(page.getByLabel('聊天', { exact: true })).toHaveCount(0)
  await page.screenshot({ path: 'test-results/lima-deleted.png', fullPage: true })
  const verify = `import asyncio\nfrom pathlib import Path\nfrom sqlalchemy import select\nfrom laxcode.config import load_config\nfrom laxcode.storage.database import Database\nfrom laxcode.storage.models import Session, Run, Message, SessionMessage\nfrom laxcode.runtime.events import EventStore\nasync def verify():\n    cfg = load_config(Path('config/config.json'))\n    async with Database.open(cfg.database) as db:\n        async with db.transaction() as tx:\n            assert (await tx.get(Session, ${session})).deleted_at is not None\n            assert (await tx.get(Run, ${third})).status == 'cancelled'\n            for model in (Message, SessionMessage):\n                assert len(list(await tx.scalars(select(model).where(model.session_id == ${session})))) == 4\n    async with EventStore.connect(cfg.nats.url) as events:\n        assert (await events.js.stream_info(events.stream_name(${third}))).state.messages > 1\n    print('Database history and NATS events retained')\nasyncio.run(verify())`
  console.log(execFileSync('ssh', ['-F', `${process.env.HOME}/.lima/${process.env.VM_NAME || 'laxcode'}/ssh.config`, '-o', 'ControlPath=none', `lima-${process.env.VM_NAME || 'laxcode'}`, 'cd ~/code/laxcode && .venv/bin/python -'], { input: verify, encoding: 'utf8', timeout: 30_000 }).trim())
  if (errors.length) throw new Error(errors.join('; '))
  console.log(JSON.stringify({ status: 'PASS', session_id: session, run_ids: runs, checks: 'real model cancellation, SSE replay, unfinished answer discarded, continued chat, active deletion, reload, retained records' }))
} catch (error) {
  mkdirSync('test-results', { recursive: true })
  await page.screenshot({ path: 'test-results/lima-control-failure.png', fullPage: true })
  throw error
} finally {
  // Stop only runs created by this smoke if an assertion failed midway.
  for (const id of runs) await request(`/runs/${id}/cancel`, 'POST').catch(() => {})
  await browser.close()
}
