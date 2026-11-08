import { expect, test, type Page } from '@playwright/test'
import type { Message, Project, Session } from '../src/api/types'

async function mockAPI(page: Page) {
  let owner = '12345678-1234-4234-8234-123456789abc'
  const state = {
    projects: [] as Project[], sessions: [] as Session[], messages: new Map<number, Message[]>(),
    posts: [] as { path: string; body: any }[], historyReads: [] as number[],
  }
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname.replace('/api/v1', '')
    const method = request.method(), body = request.postDataJSON()
    const reply = (data: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) })
    if (method === 'POST') state.posts.push({ path, body })
    if (path === '/login') {
      owner = body.user_id
      const token = `h.${Buffer.from(JSON.stringify({ sub: owner, exp: Date.now() / 1000 + 3600 })).toString('base64url')}.s`
      if (body.from_page === 'guest_login' && !state.projects.length) {
        state.projects.push({ id: 1, user_id: owner, name: 'LaxCode' })
        state.sessions.push({ id: 1, user_id: owner, project_id: 1, name: 'laxcode', next_msg_seq: 1 })
        state.messages.set(1, [])
      }
      return reply({ access_token: token, token_type: 'bearer', expires_in: 86400, initial_session_id: body.from_page ? 1 : null })
    }
    if (path === '/models') return reply({ default_model: 'p:a', models: ['a', 'b', 'c'].map(name => ({ id: `p:${name}`, provider: 'p', name })) })
    if (path === '/projects') {
      if (method === 'POST') {
        const project = { id: state.projects.length + 1, name: body.name, user_id: owner }; state.projects.push(project)
        return reply(project, 201)
      }
      return reply(state.projects)
    }
    const project = path.match(/^\/projects\/(\d+)\/sessions$/)
    if (project) {
      const id = Number(project[1])
      if (method === 'POST') {
        const session = { id: state.sessions.length + 1, user_id: owner, project_id: id, name: `会话 ${state.sessions.filter(s => s.project_id === id).length + 1}`, next_msg_seq: 1 }
        state.sessions.push(session); state.messages.set(session.id, []); return reply(session, 201)
      }
      return reply(state.sessions.filter(s => s.project_id === id))
    }
    const session = path.match(/^\/sessions\/(\d+)(\/messages|\/chat)?$/)
    if (session) {
      const id = Number(session[1]), item = state.sessions.find(s => s.id === id)
      if (!item) return reply({ detail: 'Not found' }, 404)
      if (method === 'PATCH') { item.name = body.name; return reply(item) }
      const messages = state.messages.get(id) ?? []
      if (session[2] === '/messages') {
        const before = Number(new URL(request.url()).searchParams.get('before_seq') ?? 999999)
        state.historyReads.push(before)
        const eligible = messages.filter(m => m.seq < before), items = eligible.slice(-50)
        return reply({ items, has_more: eligible.length > items.length, next_before_seq: eligible.length > items.length ? items[0].seq : null })
      }
      if (session[2] === '/chat') return reply({ run_id: 10, session_id: id, status: 'waiting' }, 202)
      return reply({ ...item, next_msg_seq: (messages.at(-1)?.seq ?? 0) + 1, latest_run: null })
    }
    return reply({ detail: 'Not found' }, 404)
  })
  return state
}

test('guest bootstrap, empty project action, rename, cookie restore and model preferences', async ({ page }) => {
  const state = await mockAPI(page)
  await page.goto('/')
  await page.getByRole('button', { name: '以游客登录' }).click()
  await expect(page.getByRole('button', { name: 'laxcode', exact: true })).toHaveAttribute('aria-current', 'page')
  await expect(page.getByRole('textbox', { name: '消息输入' })).toBeVisible()
  await expect(page.getByLabel('模型', { exact: true })).toHaveValue('p:a')
  await page.getByLabel('模型', { exact: true }).selectOption('p:b')
  await page.getByRole('button', { name: '新建', exact: true }).click()
  await page.getByRole('textbox', { name: '名称' }).fill('新的项目')
  await page.getByRole('button', { name: '确定', exact: true }).click()
  const empty = page.getByRole('region', { name: '新的项目' })
  const small = empty.getByRole('button', { name: '新建会话', exact: true })
  await expect(small).toBeVisible()
  const [buttonBox, projectBox] = await Promise.all([small.boundingBox(), empty.boundingBox()])
  expect(buttonBox!.width).toBeLessThan(projectBox!.width)
  await small.click()
  await expect(small).toHaveCount(0)
  await expect(page.getByRole('button', { name: '会话 1', exact: true })).toHaveAttribute('aria-current', 'page')
  await expect(page.getByLabel('模型', { exact: true })).toHaveValue('p:b')
  await page.getByLabel('模型', { exact: true }).selectOption('p:c')
  await page.getByRole('button', { name: '修改会话名 会话 1', exact: true }).click()
  await page.getByRole('textbox', { name: '名称' }).fill('设计讨论')
  await page.keyboard.press('Enter')
  await expect(page.getByRole('button', { name: '设计讨论', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'laxcode', exact: true }).click()
  await expect(page.getByLabel('模型', { exact: true })).toHaveValue('p:b')
  await page.getByRole('button', { name: '设计讨论', exact: true }).click()
  await page.reload()
  await expect(page.getByRole('button', { name: '设计讨论', exact: true })).toHaveAttribute('aria-current', 'page')
  await expect(page.getByLabel('模型', { exact: true })).toHaveValue('p:c')
  expect(state.posts.filter(p => p.path === '/login')).toHaveLength(1)
  await page.getByRole('button', { name: '新建', exact: true }).click()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  const input = page.getByRole('textbox', { name: '消息输入' })
  await input.fill('第一行'); await input.press('Shift+Enter'); await input.pressSequentially('第二行')
  await expect(input).toHaveValue('第一行\n第二行')
  await input.dispatchEvent('keydown', { key: 'Enter', isComposing: true })
  expect(state.posts.some(p => p.path.endsWith('/chat'))).toBe(false)
  await page.screenshot({ path: 'test-results/sidebar.png', fullPage: true })
})

test('renders markdown and tools, paginates silently and preserves reading position', async ({ page }) => {
  const state = await mockAPI(page)
  await page.goto('/'); await page.getByRole('button', { name: '以游客登录' }).click()
  await expect(page.getByRole('textbox', { name: '消息输入' })).toBeVisible()
  state.messages.set(1, Array.from({ length: 120 }, (_, i) => i % 3 === 0
    ? { seq: i + 1, role: 'user' as const, content: `问题 ${i + 1}\n第二行` }
    : i % 3 === 1 ? { seq: i + 1, role: 'assistant' as const, content: `### 回答 ${i + 1}\n\n**说明**\n\n\`\`\`ts\nconst value = ${i + 1}\n\`\`\`` }
      : { seq: i + 1, role: 'tool' as const, tool_name: 'read_file', arguments: JSON.stringify({ path: 'hello.txt', line: i + 1 }) }))
  await page.reload()
  const list = page.getByLabel('消息列表', { exact: true })
  await expect(page.locator('[data-seq="120"]')).toBeVisible()
  await expect(page.locator('[data-seq="119"] strong')).toHaveText('说明')
  await expect(page.locator('[data-seq="120"]')).toContainText('read_file')
  await expect(page.getByRole('button', { name: '加载更多' })).toHaveCount(0)
  await list.evaluate(el => { el.scrollTop = 350 })
  await expect(page.getByRole('button', { name: '回到最新' })).toBeVisible()
  await list.evaluate(el => { el.scrollTop = 80 })
  await expect.poll(() => state.historyReads.some(n => n === 71)).toBe(true)
  await expect(page.locator('[data-seq="21"]')).toBeAttached()
  const anchor = await list.evaluate(el => {
    const row = [...el.querySelectorAll<HTMLElement>('[data-seq]')].find(row => row.getBoundingClientRect().bottom > el.getBoundingClientRect().top)!
    return { seq: row.dataset.seq!, offset: row.getBoundingClientRect().top - el.getBoundingClientRect().top }
  })
  state.messages.get(1)!.push({ seq: 121, role: 'assistant', content: '新的消息' })
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await expect(page.locator('[data-seq="121"]')).toBeAttached()
  const offset = await page.locator(`[data-seq="${anchor.seq}"]`).evaluate(el => el.getBoundingClientRect().top - el.closest('[aria-label="消息列表"]')!.getBoundingClientRect().top)
  expect(Math.abs(offset - anchor.offset)).toBeLessThan(2)
  await page.getByRole('button', { name: '回到最新' }).click()
  await expect(page.locator('[data-seq="121"]')).toBeVisible()
  await expect(page.getByRole('button', { name: '回到最新' })).toHaveCount(0)
  state.messages.get(1)!.push({ seq: 122, role: 'assistant', content: '保持跟随' })
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await expect(page.locator('[data-seq="122"]')).toBeVisible()
  await page.screenshot({ path: 'test-results/messages.png', fullPage: true })
})
