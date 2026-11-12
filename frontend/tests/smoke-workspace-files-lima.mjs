// Real model + checkpoint + authenticated UI smoke against the deployed Lima service.
// Run from frontend: LAXCODE_BASE_URL=http://127.0.0.1:18000 node tests/smoke-workspace-files-lima.mjs
import { chromium, expect } from '@playwright/test'
import { existsSync, mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'

const base = process.env.LAXCODE_BASE_URL || 'http://127.0.0.1:8000'
const vm = process.env.LIMA_INSTANCE || 'laxcode'
const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || (existsSync('/Applications/Microsoft Edge.app') ? 'msedge' : undefined) })
const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, acceptDownloads: true })
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
let token, owner, project, session, runId
const paths = ['workspace-smoke.py', 'workspace-smoke.png', 'workspace-smoke.pdf']
function inVM(script) {
  return execFileSync('limactl', ['shell', vm, '--', 'bash', '-lc', 'cd ~/code/laxcode && .venv/bin/python -c "$1"', 'smoke', script], { encoding: 'utf8', timeout: 120_000 })
}
async function request(path, method = 'GET', data) {
  const response = await context.request.fetch(`${base}/api/v1${path}`, { method, data, headers: { Authorization: `Bearer ${token}` } })
  if (!response.ok()) throw new Error(`${method} ${path}: ${response.status()} ${await response.text()}`)
  return response
}
async function json(path) { return (await request(path)).json() }
try {
  const existingOwner = process.env.LAXCODE_SMOKE_USER_ID
  if (existingOwner) await context.addCookies([{ name: 'user_id', value: existingOwner, url: base }])
  await page.goto(base)
  if (!existingOwner) await page.getByRole('button', { name: '以游客登录' }).click()
  else await page.getByRole('button', { name: 'LaxCode', exact: true }).click()
  await expect(page.getByRole('textbox', { name: '消息输入' })).toBeVisible()
  const cookies = await context.cookies()
  token = decodeURIComponent(cookies.find(c => c.name === 'access_token').value)
  owner = decodeURIComponent(cookies.find(c => c.name === 'user_id').value)
  project = (await json('/projects'))[0].id
  session = (await json(`/projects/${project}/sessions`))[0].id
  const query = `执行文件展示验收，不要提问。目标都是项目 workspace 文件，不是技能文件。为避免模型把 null 误用为字符串，本次 write_file/edit_file 参数中完全省略 skill_name 字段（后端默认 workspace）。禁止使用字符串 "null" 或创建技能目录。
依次执行：
1. write_file 参数严格使用 {"path":"workspace-smoke.py","operation":"write","content":"print(\\"WORKSPACE_SMOKE_V1\\")\\n"}，不要添加 skill_name。
2. edit_file 参数使用 {"path":"workspace-smoke.py","edits":[{"old_text":"V1","new_text":"V2"}]}，不要添加 skill_name。
3. 用 bash 执行 cp .workspace-smoke-source.png workspace-smoke.png，已有源文件是验收用有效 PNG。然后同一 bash 调用使用 printf '%s\\n' '%PDF-1.4' '%%EOF' > workspace-smoke.pdf，仅用于下载测试。
4. 调用 list_written_files 一次，核对候选清单应只包含 workspace-smoke.py。如果不符合则直接报告错误，不要反复尝试修复。
5. 最终简短回答，依次插入下面三个各自独立段落的原样链接，不要放进代码块或列表：
[代码文件](workspace:/workspace-smoke.py)
[生成图片](workspace:/workspace-smoke.png)
[下载文档](workspace:/workspace-smoke.pdf)
最后补一行：也可以在行内打开 [入口代码](workspace:/workspace-smoke.py)。`
  if (existingOwner) {
    runId = Number(process.env.LAXCODE_SMOKE_RUN_ID)
    if (!runId) throw new Error('Existing smoke user requires LAXCODE_SMOKE_RUN_ID')
  } else {
    inVM(`from laxcode.layout import get_project_workdir\nfrom PIL import Image\np=get_project_workdir(${JSON.stringify(owner)},${project})\np.mkdir(parents=True,exist_ok=True)\nImage.new('RGB',(64,32),'blue').save(p/'.workspace-smoke-source.png')`)
    await page.getByRole('textbox', { name: '消息输入' }).fill(query)
    const submitted = page.waitForResponse(r => r.url().endsWith(`/sessions/${session}/chat`) && r.request().method() === 'POST')
    await page.getByRole('button', { name: '发送消息', exact: true }).click()
    runId = (await (await submitted).json()).run_id
    console.log(JSON.stringify({ progress: 'submitted', user_id: owner, session_id: session, run_id: runId }))
  }
  const deadline = Date.now() + 360_000
  let result
  while (Date.now() < deadline) {
    result = await json(`/runs/${runId}`)
    if (['success', 'failed', 'cancelled'].includes(result.status)) break
    await new Promise(resolve => setTimeout(resolve, 1000))
  }
  if (result.status !== 'success') throw new Error(`Run failed: ${result.status} ${result.error_code} ${result.error_desc}`)
  for (const path of paths) await expect(page.getByRole('region', { name: `文件 ${path}`, exact: true })).toBeVisible({ timeout: 20000 })
  const code = page.getByRole('region', { name: `文件 ${paths[0]}`, exact: true })
  await expect(code).toContainText('WORKSPACE_SMOKE_V2')
  const image = page.getByRole('region', { name: `文件 ${paths[1]}`, exact: true }).getByRole('img')
  await expect(image).toBeVisible()
  await expect.poll(() => image.evaluate(el => el.naturalWidth)).toBe(64)
  await code.getByRole('button', { name: `展开 ${paths[0]}` }).click()
  await expect(page.getByRole('dialog')).toContainText('WORKSPACE_SMOKE_V2')
  await page.getByRole('button', { name: '关闭文件预览' }).click()
  await page.getByRole('button', { name: '入口代码', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.getByRole('button', { name: '关闭文件预览' }).click()
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: `下载 ${paths[2]}`, exact: true }).click()
  const saved = await download
  if (saved.suggestedFilename() !== paths[2]) throw new Error('Wrong download name')
  const checkpoint = inVM(`import asyncio, json\nfrom pathlib import Path\nfrom laxcode.config import load_config\nfrom laxcode.storage.database import Database\nfrom laxcode.storage.repositories import Repository\nfrom laxcode.storage.checkpoints import open_checkpointer\nasync def check():\n c=load_config(Path('config/config.json'))\n async with Database.open(c.database) as db, open_checkpointer(c.database) as saver:\n  r=await Repository(db).get_run(${runId}, ${JSON.stringify(owner)})\n  snap=await saver.aget_tuple({'configurable': {'thread_id': r.thread_id}})\n  state=snap.checkpoint['channel_values']\n  assert state['written_workspace_paths']==['workspace-smoke.py'], state['written_workspace_paths']\n  tools=[m for m in state['messages'] if m['role']=='tool']\n  assert {'write_file','edit_file','bash','list_written_files'} <= {m['tool_name'] for m in tools}\n  listed=[m for m in tools if m['tool_name']=='list_written_files'][-1]\n  assert json.loads(listed['content'])['result']['paths']==['workspace-smoke.py']\n  print('PASS real tools and persisted candidate paths')\nasyncio.run(check())`)
  console.log(checkpoint.trim())
  inVM(`from laxcode.layout import get_project_workdir\np=get_project_workdir(${JSON.stringify(owner)},${project})/'workspace-smoke.py'\np.write_text('print("WORKSPACE_SMOKE_V3")\\n')`)
  await code.getByRole('button', { name: `刷新 ${paths[0]}` }).click()
  await expect(code).toContainText('WORKSPACE_SMOKE_V3')
  await page.reload()
  await expect(page.getByRole('region', { name: `文件 ${paths[0]}`, exact: true })).toContainText('WORKSPACE_SMOKE_V3', { timeout: 20000 })
  mkdirSync('test-results', { recursive: true })
  await page.screenshot({ path: 'test-results/lima-workspace-files.png', fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: 'test-results/lima-workspace-files-mobile.png', fullPage: true })
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('Mobile horizontal overflow')
  inVM(`from laxcode.layout import get_project_workdir\n(get_project_workdir(${JSON.stringify(owner)},${project})/'workspace-smoke.pdf').unlink()`)
  await page.getByRole('button', { name: `刷新 ${paths[2]}` }).click()
  await expect(page.getByRole('region', { name: `文件 ${paths[2]}`, exact: true })).toContainText('文件不存在')
  const invalid = await context.request.get(`${base}/api/v1/sessions/${session}/workspace-files/preview?path=..%2Fconfig%2Fconfig.json`, { headers: { Authorization: `Bearer ${token}` } })
  if (invalid.status() !== 400) throw new Error('Traversal accepted')
  if (errors.length) throw new Error(errors.join('\n'))
  console.log(JSON.stringify({ status: 'PASS', run_id: runId, session_id: session,
    checks: ['real LLM write/edit/bash/list', 'PostgreSQL checkpoint', 'text and image cards', 'inline dialog', 'download', 'refresh current content', 'history reload', 'mobile layout', 'missing file', 'path boundary'] }))
} finally {
  if (runId) {
    try {
      const run = await json(`/runs/${runId}`)
      if (['waiting', 'running', 'paused'].includes(run.status)) await request(`/runs/${runId}/cancel`, 'POST')
    } catch { /* Preserve original smoke failure. */ }
  }
  await browser.close()
}
