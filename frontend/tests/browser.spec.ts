import { expect, test, type Page } from '@playwright/test'
import type { MemoryItem, Message, Project, Run, Session } from '../src/api/types'

async function mockAPI(page: Page) {
  let owner = '12345678-1234-4234-8234-123456789abc'
  const state = {
    configs: {} as Record<string, string>, configFailures: 0, memoryFailures: 0, memoryDeleteFailures: 0,
    memories: [] as (MemoryItem & { category: string })[],
    projects: [] as Project[], sessions: [] as Session[], messages: new Map<number, Message[]>(),
    posts: [] as { path: string; body: any }[], historyReads: [] as number[],
    runs: new Map<number, Run>(), deletions: [] as number[], deleteFailures: 0, cancelFailures: 0,
    streams: new Map<number, (() => void)[]>(),
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
    if (path === '/user-configs') {
      if (state.configFailures-- > 0) return reply({ detail: '配置保存失败，请重试' }, 503)
      if (method === 'PATCH') state.configs[body.key] = body.value
      return reply({ configs: state.configs })
    }
    if (path === '/memories' || path.startsWith('/memories/')) {
      if (method === 'DELETE') {
        if (state.memoryDeleteFailures-- > 0) return reply({ detail: '记忆删除失败，请重试' }, 503)
        state.memories = path === '/memories' ? [] : state.memories.filter(item => item.id !== Number(path.split('/').at(-1)))
        return route.fulfill({ status: 204 })
      }
      if (state.memoryFailures-- > 0) return reply({ detail: '记忆加载失败，请重试' }, 503)
      return reply({ categories: Object.entries({ profile: '用户背景', preference: '长期偏好', instruction: '行为要求', knowledge: '业务知识', project: '项目背景', workflow: '工作方法' }).map(([category, label]) => ({
        category, label, items: state.memories.filter(item => item.category === category).map(({ category: _, ...item }) => item),
      })) })
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
    const runRoute = path.match(/^\/runs\/(\d+)\/(cancel|resume|events)$/)
    if (runRoute) {
      const id = Number(runRoute[1]), run = state.runs.get(id)
      if (!run) return reply({ detail: 'Not found' }, 404)
      if (runRoute[2] === 'resume') {
        if (!['failed', 'cancelled'].includes(run.status) || body.resume_time !== run.resume_time) return reply({ detail: '状态已变化' }, 409)
        run.status = 'running'; run.resume_time++; run.error = run.error_code = run.error_desc = null
        return reply({ run_id: id, session_id: run.session_id, status: 'waiting', resume_time: run.resume_time }, 202)
      }
      if (runRoute[2] === 'cancel') {
        if (state.cancelFailures-- > 0) return reply({ detail: '取消失败，请重试' }, 503)
        run.status = 'cancelled'
        state.streams.get(id)?.forEach(end => end())
        return reply(run)
      }
      if (run.status === 'running' || run.status === 'waiting') await new Promise<void>(resolve => {
        state.streams.set(id, [...(state.streams.get(id) ?? []), resolve])
      })
      const event = { run_id: id, resume_time: run.resume_time, msg_seq: null, type: 'sse_end', role: 'system', error: null, content: { status: run.status } }
      return route.fulfill({ contentType: 'text/event-stream', body: `event: sse_end\ndata: ${JSON.stringify(event)}\n\n` })
    }
    const session = path.match(/^\/sessions\/(\d+)(\/messages|\/chat)?$/)
    if (session) {
      const id = Number(session[1]), item = state.sessions.find(s => s.id === id)
      if (!item) return reply({ detail: 'Not found' }, 404)
      if (method === 'DELETE') {
        state.deletions.push(id)
        if (state.deleteFailures-- > 0) return reply({ detail: '删除失败，请重试' }, 503)
        state.sessions = state.sessions.filter(s => s.id !== id)
        for (const run of state.runs.values()) if (run.session_id === id) {
          run.status = 'cancelled'; state.streams.get(run.run_id)?.forEach(end => end())
        }
        return route.fulfill({ status: 204 })
      }
      if (method === 'PATCH') { item.name = body.name; return reply(item) }
      const messages = state.messages.get(id) ?? []
      if (session[2] === '/messages') {
        const before = Number(new URL(request.url()).searchParams.get('before_seq') ?? 999999)
        state.historyReads.push(before)
        const eligible = messages.filter(m => m.seq < before), items = eligible.slice(-50)
        return reply({ items, has_more: eligible.length > items.length, next_before_seq: eligible.length > items.length ? items[0].seq : null })
      }
      if (session[2] === '/chat') {
        const run: Run = { run_id: state.runs.size + 10, session_id: id, status: 'running', model: body.model, error: null, error_code: null, error_desc: null, resume_time: 0 }
        state.runs.set(run.run_id, run)
        messages.push({ role: 'user', seq: (messages.at(-1)?.seq ?? 0) + 1, content: body.user_query })
        return reply({ run_id: run.run_id, session_id: id, status: 'waiting', resume_time: 0 }, 202)
      }
      return reply({ ...item, next_msg_seq: (messages.at(-1)?.seq ?? 0) + 1, latest_run: [...state.runs.values()].filter(r => r.session_id === id).at(-1) ?? null })
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
  await page.locator('[data-anchor="work:119"]').getByRole('button', { name: '已工作' }).click()
  await expect(page.locator('[data-seq="120"]')).toBeVisible()
  await expect(page.locator('[data-seq="119"] strong')).toHaveText('说明')
  await expect(page.locator('[data-seq="120"]')).toContainText('read_file')
  await expect(page.getByRole('button', { name: '加载更多' })).toHaveCount(0)
  await list.evaluate(el => { el.scrollTop = 350 })
  await expect(page.getByRole('button', { name: '回到最新' })).toBeVisible()
  await list.evaluate(el => { el.scrollTop = 80 })
  await expect.poll(() => state.historyReads.some(n => n === 71)).toBe(true)
  await expect(page.locator('[data-anchor="work:start"]')).toBeAttached()
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

test('streams nested reasoning disclosures and preserves expansion when history replaces live data', async ({ page }) => {
  await page.addInitScript(() => {
    const original = window.fetch.bind(window)
    window.fetch = (input, init) => {
      if (String(input).match(/\/runs\/\d+\/events(?:\?|$)/)) {
        return Promise.resolve(new Response(new ReadableStream({ start(controller) {
          ;(window as any).reasoningEvent = (event: unknown) => controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`))
          init?.signal?.addEventListener('abort', () => { try { controller.close() } catch { /* Already cancelled */ } })
        } }), { headers: { 'Content-Type': 'text/event-stream' } }))
      }
      return original(input, init)
    }
  })
  const state = await mockAPI(page)
  await page.goto('/')
  await page.getByRole('button', { name: '以游客登录' }).click()
  await page.getByRole('textbox', { name: '消息输入' }).fill('检查文件并说明结果')
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
  await page.waitForFunction(() => typeof (window as any).reasoningEvent === 'function')
  const emit = async (type: string, msg_seq: number | null, content: unknown) => page.evaluate(event => {
    ;(window as any).reasoningEvent(event)
  }, { run_id: 10, type, msg_seq, content, role: 'assistant', error: null, channel: 'text' })
  await emit('reasoning_chunk', 2, '先检查 **消息链路**。')
  const work = page.getByRole('button', { name: '正在工作', exact: true })
  await expect(work).toHaveAttribute('aria-expanded', 'false')
  await expect(work).toHaveText('正在工作')
  await emit('reasoning_chunk', 2, '\n')
  await expect(work).toContainText('正在工作(先检查 **消息链路**。)')
  const longLine = '正在检查消息存储和流式事件，确认每条完整内容都能按顺序显示。'.repeat(5)
  await emit('reasoning_chunk', 2, `${longLine}\n未完成的尾行`)
  await page.setViewportSize({ width: 600, height: 900 })
  const preview = work.getByTitle(longLine).locator('span')
  await expect(preview).toBeVisible()
  expect(await preview.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true)
  await expect(preview).toHaveCSS('text-overflow', 'ellipsis')
  await expect(work).not.toContainText('未完成的尾行')
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await expect(preview).toHaveCSS('animation-name', 'none')
  await page.screenshot({ path: 'test-results/work-preview.png', fullPage: true })
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await page.setViewportSize({ width: 1440, height: 900 })
  await work.click()
  await expect(work).toHaveText('正在工作')
  const thinking = page.getByRole('button', { name: '正在思考', exact: true })
  await expect(thinking).toHaveAttribute('aria-expanded', 'false')
  await thinking.focus(); await page.keyboard.press('Enter')
  await emit('reasoning_chunk', 2, '\n\n再核对历史消息与流式事件。')
  await expect(page.getByText('消息链路', { exact: true })).toBeVisible()
  await expect(page.getByText('再核对历史消息与流式事件。')).toBeVisible()
  await emit('message_chunk', 2, '我先读取相关文件，核对 reasoning 的传递过程。')
  await expect(page.getByRole('button', { name: '已工作', exact: true })).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByRole('button', { name: '已思考', exact: true })).toHaveAttribute('aria-expanded', 'true')
  const reasoning = [{ channel: 'text' as const, text: '先检查 **消息链路**。\n\n再核对历史消息与流式事件。' }]
  const content = '我先读取相关文件，核对 reasoning 的传递过程。'
  await emit('message_end', 2, { text: content, reasoning })
  await emit('tool_start', 3, { tool_name: 'read_file', arguments: '{"path":"example.py"}' })
  await page.getByRole('button', { name: '正在工作', exact: true }).click()
  await page.getByRole('button', { name: 'read_file' }).click()
  await emit('tool_end', 3, { result: 'not displayed' })
  await emit('reasoning_chunk', 4, '已找到需要补齐的字段。')
  await page.getByRole('button', { name: '正在思考' }).click()
  await emit('reasoning_chunk', 4, '接下来汇总结果。')
  await expect(page.getByText('已找到需要补齐的字段。接下来汇总结果。')).toBeVisible()
  await page.screenshot({ path: 'test-results/reasoning-stream.png', fullPage: true })
  state.messages.get(1)!.push({ seq: 2, role: 'assistant', content, reasoning },
    { seq: 3, role: 'tool', tool_name: 'read_file', arguments: '{"path":"example.py"}' },
    { seq: 4, role: 'assistant', content: '检查完成。', reasoning: [{ channel: 'text', text: '已找到需要补齐的字段。接下来汇总结果。' }] })
  state.runs.get(10)!.status = 'success'
  await emit('sse_end', null, { status: 'completed' })
  await expect(page.getByText('检查完成。')).toBeVisible()
  for (const button of await page.getByRole('button', { name: '已工作', exact: true }).all()) {
    await expect(button).toHaveAttribute('aria-expanded', 'true')
  }
  await expect(page.getByText('已找到需要补齐的字段。接下来汇总结果。')).toBeVisible()
  await expect(page.getByText('not displayed')).toHaveCount(0)
  await page.reload()
  await expect(page.getByRole('button', { name: '已工作', exact: true })).toHaveCount(2)
  for (const button of await page.getByRole('button', { name: '已工作', exact: true }).all()) {
    await expect(button).toHaveAttribute('aria-expanded', 'false')
  }
})

test('delete dialog confirms once, retries errors and clears only the deleted selection', async ({ page }) => {
  const state = await mockAPI(page)
  await page.goto('/'); await page.getByRole('button', { name: '以游客登录' }).click()
  await page.getByRole('button', { name: 'LaxCode 新会话', exact: true }).click()
  await expect(page.getByRole('button', { name: '会话 2', exact: true })).toHaveAttribute('aria-current', 'page')
  await page.getByRole('button', { name: '删除会话 laxcode', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('button', { name: '取消', exact: true })).toBeFocused()
  await page.screenshot({ path: 'test-results/delete-confirm.png', fullPage: true })
  await page.keyboard.press('Escape')
  expect(state.deletions).toEqual([])
  await page.getByRole('button', { name: '删除会话 laxcode', exact: true }).click()
  state.deleteFailures = 1
  await dialog.getByRole('button', { name: '删除', exact: true }).click()
  await expect(dialog.getByRole('alert')).toContainText('删除失败')
  await expect(page.getByRole('button', { name: 'laxcode', exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: '删除', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'laxcode', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '会话 2', exact: true })).toHaveAttribute('aria-current', 'page')
  await page.getByRole('button', { name: '删除会话 会话 2', exact: true }).click()
  await dialog.getByRole('button', { name: '删除', exact: true }).click()
  await expect(page.getByLabel('聊天', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '新建会话', exact: true })).toBeVisible()
  const preferences = (await page.context().cookies()).find(c => c.name === 'laxcode_preferences')!
  expect(JSON.parse(decodeURIComponent(preferences.value))).toMatchObject({ selected: null, models: [] })
  await page.reload()
  await expect(page.getByLabel('聊天', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '新建会话', exact: true })).toBeVisible()
})

test('composer cancels without confirmation and delete stops a running session', async ({ page }) => {
  const state = await mockAPI(page)
  await page.goto('/'); await page.getByRole('button', { name: '以游客登录' }).click()
  await page.getByRole('textbox', { name: '消息输入' }).fill('hello')
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
  await expect(page.getByRole('button', { name: '取消生成', exact: true })).toBeEnabled()
  await page.getByRole('textbox', { name: '消息输入' }).fill('draft')
  await page.getByRole('textbox', { name: '消息输入' }).press('Enter')
  expect(state.posts.filter(p => p.path.endsWith('/cancel'))).toHaveLength(0)
  expect(state.posts.filter(p => p.path.endsWith('/chat'))).toHaveLength(1)
  state.cancelFailures = 1
  await page.getByRole('button', { name: '取消生成', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('取消失败')
  await page.getByRole('button', { name: '取消生成', exact: true }).click()
  await expect(page.getByRole('status')).toHaveText('已取消')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeEnabled()
  await page.reload()
  await expect(page.getByRole('status')).toHaveText('已取消')
  await page.getByRole('textbox', { name: '消息输入' }).fill('continue')
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
  await expect(page.getByRole('button', { name: '取消生成', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '删除会话 laxcode', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '删除', exact: true }).click()
  await expect(page.getByLabel('聊天', { exact: true })).toHaveCount(0)
  expect(state.runs.get(11)?.status).toBe('cancelled')
  expect(state.posts.filter(p => p.path.endsWith('/cancel'))).toHaveLength(2)
})


test('memory switch persists across sessions and reload, with save failure recovery', async ({ page }) => {
  const state = await mockAPI(page)
  await page.goto('/')
  await page.getByRole('button', { name: '以游客登录' }).click()
  const toggle = page.getByRole('switch', { name: '记忆', exact: true })
  await expect(toggle).toBeEnabled()
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  state.configFailures = 1
  await toggle.click()
  await expect(page.getByRole('alert')).toContainText('配置保存失败')
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  expect(state.configs.enable_memory).toBe('1')
  await page.getByRole('button', { name: 'LaxCode 新会话', exact: true }).click()
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await page.reload()
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await page.getByRole('button', { name: '查看记忆', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '用户记忆', exact: true })).toBeVisible()
})

test('memory drawer groups, full text, deletion failures, focus and responsive layout', async ({ page }) => {
  const state = await mockAPI(page)
  const longText = '<script>alert(1)</script> 长期项目的完整背景、范围和条件。'.repeat(30)
  state.memories = [
    { id: 10, category: 'profile', abstract: '用户背景摘要'.repeat(30), detail: longText },
    { id: 11, category: 'preference', abstract: '偏好摘要', detail: '偏好详情' },
  ]
  await page.goto('/')
  await page.getByRole('button', { name: '以游客登录' }).click()
  const open = page.getByRole('button', { name: '查看记忆', exact: true })
  await open.click()
  const drawer = page.getByRole('dialog', { name: '用户记忆', exact: true })
  await expect(drawer).toBeVisible()
  await expect(drawer.getByRole('region')).toHaveCount(6)
  const box = await drawer.boundingBox()
  expect(box!.width).toBeCloseTo(480, 0)
  const preview = drawer.getByRole('button', { name: '记忆 10 详情', exact: true })
  expect(await preview.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true)
  await preview.hover()
  const tooltip = page.getByRole('tooltip').filter({ hasText: '<script>alert(1)</script>' })
  await expect(tooltip).toBeVisible()
  await expect(tooltip).toHaveText(longText)
  await preview.focus()
  await expect(tooltip).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(drawer).toBeVisible()
  await drawer.getByRole('button', { name: '删除记忆 10', exact: true }).click()
  let confirmation = page.getByRole('dialog', { name: '删除记忆 10？', exact: true })
  await expect(confirmation).toBeVisible()
  await expect(confirmation).toContainText('运行中的任务仍可能生成新记忆')
  await confirmation.getByRole('button', { name: '取消', exact: true }).click()
  expect(state.memories).toHaveLength(2)
  await drawer.getByRole('button', { name: '删除记忆 10', exact: true }).click()
  state.memoryDeleteFailures = 1
  await confirmation.getByRole('button', { name: '确认删除', exact: true }).click()
  await expect(confirmation.getByRole('alert')).toContainText('记忆删除失败')
  expect(state.memories).toHaveLength(2)
  await confirmation.getByRole('button', { name: '确认删除', exact: true }).click()
  await expect(drawer.getByRole('button', { name: '删除记忆 10', exact: true })).toHaveCount(0)
  await drawer.getByRole('button', { name: '全部删除', exact: true }).click()
  confirmation = page.getByRole('dialog', { name: '删除全部记忆？', exact: true })
  await confirmation.getByRole('button', { name: '确认删除', exact: true }).click()
  await expect(drawer.getByText('0 条记忆', { exact: true })).toBeVisible()
  await expect(drawer.getByRole('button', { name: '全部删除', exact: true })).toBeDisabled()
  await page.keyboard.press('Escape')
  await expect(drawer).toHaveCount(0)
  await expect(open).toBeFocused()
  await page.setViewportSize({ width: 390, height: 844 })
  state.memories = [{ id: 12, category: 'profile', abstract: '小屏摘要', detail: longText }]
  await open.click()
  await expect(drawer).toBeVisible()
  await expect(drawer.getByRole('button', { name: '记忆 12 详情', exact: true })).toBeVisible()
  expect((await drawer.boundingBox())!.width).toBe(390)
  await drawer.getByRole('button', { name: '记忆 12 详情', exact: true }).click()
  await expect(page.getByRole('tooltip').filter({ hasText: '<script>alert(1)</script>' })).toBeVisible()
  await page.screenshot({ path: 'test-results/memory-drawer-mobile.png' })
})

test('memory management remains available without a session and supports load retry', async ({ page }) => {
  const state = await mockAPI(page)
  await page.goto('/')
  await page.getByRole('button', { name: '以游客登录' }).click()
  await expect(page.getByRole('textbox', { name: '消息输入' })).toBeVisible()
  state.sessions = []
  state.configFailures = 1
  state.memoryFailures = 1
  await page.reload()
  await expect(page.getByRole('button', { name: '重试配置', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '重试配置', exact: true }).click()
  await expect(page.getByRole('switch', { name: '记忆', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '查看记忆', exact: true }).click()
  const drawer = page.getByRole('dialog', { name: '用户记忆', exact: true })
  await expect(drawer.getByRole('alert')).toContainText('记忆加载失败')
  await drawer.getByRole('button', { name: '重试加载', exact: true }).click()
  await expect(drawer.getByText('0 条记忆', { exact: true })).toBeVisible()
  await expect(drawer.getByRole('region')).toHaveCount(6)
  state.memories = [{ id: 8, category: 'workflow', abstract: '操作方法', detail: '操作方法详情' }]
  await drawer.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(drawer.getByRole('button', { name: '记忆 8 摘要', exact: true })).toBeVisible()
})

test('resumes cancelled and failed runs with the same id and keeps the composer draft', async ({ page }) => {
  const state = await mockAPI(page)
  await page.goto('/')
  await page.getByRole('button', { name: '以游客登录' }).click()
  const input = page.getByRole('textbox', { name: '消息输入' })
  await input.fill('需要继续的任务')
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
  await page.getByRole('button', { name: '取消生成', exact: true }).click()
  await expect(page.getByRole('button', { name: '继续', exact: true })).toBeVisible()
  await expect(page.getByRole('status')).toHaveText('已取消')
  await input.fill('继续编辑草稿')
  await page.getByRole('button', { name: '继续', exact: true }).click()
  await expect(page.getByRole('button', { name: '取消生成', exact: true })).toBeVisible()
  await expect(input).toHaveValue('继续编辑草稿')
  expect(state.runs.get(10)?.resume_time).toBe(1)
  await page.getByRole('button', { name: '取消生成', exact: true }).click()
  await expect(page.getByRole('button', { name: '继续', exact: true })).toBeVisible()
  Object.assign(state.runs.get(10)!, { status: 'failed', error: 'Model stream ended without response.completed',
    error_code: 'model_stream_ended', error_desc: '模型回答意外中断，请重试。' })
  await page.reload()
  await expect(page.getByRole('button', { name: '重试', exact: true })).toBeVisible()
  await expect(page.getByRole('status')).toHaveText('模型回答意外中断，请重试。')
  await expect(page.getByText('Model stream ended without response.completed', { exact: true })).toHaveCount(0)
  await page.screenshot({ path: 'test-results/resume-failed.png', fullPage: true })
  await page.getByRole('button', { name: '重试', exact: true }).click()
  await expect(page.getByRole('button', { name: '取消生成', exact: true })).toBeVisible()
  expect(state.runs.get(10)?.resume_time).toBe(2)
  expect(state.posts.filter(post => post.path === '/runs/10/resume').map(post => post.body)).toEqual([{ resume_time: 0 }, { resume_time: 1 }])
  expect(state.messages.get(1)?.filter(message => message.role === 'user')).toHaveLength(1)
})
