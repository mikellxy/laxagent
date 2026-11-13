// Real Lima service, fresh guest and real LLM. Retains this user's smoke evidence.
// Run from frontend/ after scripts/deploy_lima.sh and starting the service.
import { chromium, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync } from 'node:fs'

const base = process.env.LAXAGENT_BASE_URL || 'http://127.0.0.1:8000'
const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || (existsSync('/Applications/Microsoft Edge.app') ? 'msedge' : undefined) })
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage(), errors = []
page.on('pageerror', error => errors.push(error.message))
let token, runId
async function get(path) {
  const response = await context.request.get(`${base}/api/v1${path}`, { headers: { Authorization: `Bearer ${token}` } })
  expect(response.ok()).toBe(true)
  return response.json()
}
try {
  mkdirSync('test-results', { recursive: true })
  await page.goto(base)
  const loginResponse = page.waitForResponse(response => response.url().endsWith('/api/v1/login'))
  await page.getByRole('button', { name: '以游客登录' }).click()
  const login = await (await loginResponse).json()
  token = login.access_token
  const session = login.initial_session_id
  const owner = decodeURIComponent((await context.cookies()).find(cookie => cookie.name === 'user_id').value)
  const skills = (await get('/skills')).skills
  expect(skills.some(skill => skill.name === 'laxagent')).toBe(true)
  expect(skills.every(skill => Object.keys(skill).sort().join(',') === 'desc,name')).toBe(true)
  const detail = await get(`/sessions/${session}`)
  execFileSync('limactl', ['shell', process.env.VM_NAME || 'laxagent', '--', 'bash', '-lc',
    'cd ~/code/laxagent && .venv/bin/python -c "$1" "$2"', 'skill-ui-smoke',
    'import json,sys; from laxagent.layout import get_skills_dir,get_project_workdir; u,p=json.loads(sys.argv[1]); assert (get_skills_dir(u)/"laxagent"/"SKILL.md").is_file(); assert not get_project_workdir(u,p).exists()',
    JSON.stringify([owner, detail.project_id])], { timeout: 30000 })
  const input = page.getByRole('textbox', { name: '消息输入' })
  await input.fill('/')
  await expect(page.getByRole('listbox', { name: '选择技能' }).getByRole('option')).toHaveCount(skills.length)
  await page.screenshot({ path: 'test-results/lima-skills-menu.png' })
  await input.pressSequentially('LXA')
  await expect(page.getByRole('listbox').getByRole('option', { name: /^laxagent / })).toHaveAttribute('aria-selected', 'true')
  await input.press('Enter')
  await expect(input.locator('[data-skill-raw]')).toHaveText('laxagent')
  const suffix = '请先加载这个技能，再只回复“技能选择验收完成”。不调用其他工具。'
  await input.pressSequentially(suffix)
  await page.screenshot({ path: 'test-results/lima-skills-draft.png' })
  const submitted = page.waitForResponse(response => response.url().endsWith(`/sessions/${session}/chat`) && response.request().method() === 'POST')
  await page.getByRole('button', { name: '发送消息' }).click()
  const response = await submitted
  expect(response.status()).toBe(202)
  expect(response.request().postDataJSON().user_query).toBe(` 用户指定使用技能laxagent ${suffix}`)
  runId = (await response.json()).run_id
  await expect.poll(async () => (await get(`/runs/${runId}`)).status, { timeout: 240000, intervals: [500, 1000, 2000] }).not.toMatch(/^(waiting|running)$/)
  expect((await get(`/runs/${runId}`)).status).toBe('success')
  const history = (await get(`/sessions/${session}/messages?limit=50`)).items
  expect(history.find(message => message.role === 'user').content).toBe(` 用户指定使用技能laxagent ${suffix}`)
  expect(history.some(message => message.role === 'tool' && message.tool_name === 'load_skill')).toBe(true)
  await page.reload()
  await expect(page.getByRole('article', { name: '用户消息', exact: true }).locator('[data-skill-raw]')).toHaveText('laxagent')
  await page.screenshot({ path: 'test-results/lima-skills-history.png' })
  await page.setViewportSize({ width: 390, height: 844 })
  await input.fill('/')
  await expect(page.getByRole('listbox').getByRole('option').first()).toBeVisible()
  await page.screenshot({ path: 'test-results/lima-skills-mobile.png' })
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false)
  expect(errors).toEqual([])
  console.log(JSON.stringify({ status: 'PASS', user_id: owner, session_id: session, run_id: runId,
    checks: ['login seeds skills', 'catalog', 'picker', 'literal request and history', 'real load_skill', 'mobile'] }))
} finally {
  if (runId) {
    const run = await get(`/runs/${runId}`).catch(() => null)
    if (run && ['waiting', 'running', 'paused'].includes(run.status)) {
      await context.request.post(`${base}/api/v1/runs/${runId}/cancel`, { headers: { Authorization: `Bearer ${token}` } })
    }
  }
  await browser.close()
}
