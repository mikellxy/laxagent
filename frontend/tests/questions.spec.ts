import { expect, test, type Page } from '@playwright/test'

async function mockQuestions(page: Page, questions = [{ question: '使用哪种数据库？', choice: ['PostgreSQL', 'SQLite'] }, { question: '补充说明？', choice: [] }]) {
  const owner = '12345678-1234-4234-8234-123456789abc'
  let status = 'paused', resumeTime = 0
  let answers: { question: string; answer: string }[] = []
  const run = () => ({ run_id: 1, session_id: 1, status, resume_time: resumeTime, model: 'p:a', error: null, error_code: null, error_desc: null })
  const session = { id: 1, project_id: 1, user_id: owner, name: '问答验收', next_msg_seq: 3 }
  const history = () => [{ seq: 1, role: 'user', content: '请先询问我' }, { seq: 2, role: 'assistant', content: '' },
    ...(status === 'success' ? [{ seq: 3, role: 'tool', tool_name: 'ask_user_question', arguments: JSON.stringify({ questions }), answers },
      { seq: 4, role: 'assistant', content: '已收到你的回答。' }] : [])]
  const event = (type: string, content: unknown, seq: number | null = null) => `event: ${type}\ndata: ${JSON.stringify({
    run_id: 1, resume_time: resumeTime, msg_seq: seq, role: 'tool', error: null, type, content,
  })}\n\n`
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname.replace('/api/v1', '')
    const reply = (body: unknown, code = 200) => route.fulfill({ status: code, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/login') {
      const token = `h.${Buffer.from(JSON.stringify({ sub: owner, exp: Date.now() / 1000 + 3600 })).toString('base64url')}.s`
      return reply({ access_token: token, token_type: 'bearer', expires_in: 3600, initial_session_id: 1 })
    }
    if (path === '/projects') return reply([{ id: 1, user_id: owner, name: 'LaxCode' }])
    if (path === '/projects/1/sessions') return reply([session])
    if (path === '/models') return reply({ default_model: 'p:a', models: [{ id: 'p:a', provider: 'p', name: 'a' }] })
    if (path === '/user-configs') return reply({ configs: {} })
    if (path === '/sessions/1') return reply({ ...session, next_msg_seq: status === 'success' ? 5 : 3, latest_run: run() })
    if (path === '/sessions/1/messages') return reply({ items: history(), has_more: false, next_before_seq: null })
    if (path === '/runs/1') return reply(run())
    if (path === '/runs/1/cancel') { status = 'cancelled'; return reply(run()) }
    if (path === '/runs/1/answers') {
      const body = request.postDataJSON()
      expect(body.resume_time).toBe(0); expect(body.interrupt_id).toBe('question-1')
      answers = body.answers; resumeTime = 1; status = 'running'
      return reply({ run_id: 1, session_id: 1, status: 'waiting', resume_time: 1 }, 202)
    }
    if (path === '/runs/1/events') {
      let body: string
      if (resumeTime === 0) {
        body = event('tool_call_start', { tool_name: 'ask_user_question', tool_call_id: 'call' }, 2)
          + event('tool_start', { tool_name: 'ask_user_question', tool_call_id: 'call', arguments: JSON.stringify({ questions }) }, 3)
          + event('ask_user_question', { interrupt_id: 'question-1', tool_call_id: 'call', questions })
          + event('sse_end', { status: 'paused' })
      } else {
        body = event('tool_end', { tool_name: 'ask_user_question', tool_call_id: 'call', error_code: null, result: { answers } }, 3)
          + event('message_end', { text: '已收到你的回答。' }, 4) + event('sse_end', { status: 'completed' })
        status = 'success'
      }
      return route.fulfill({ contentType: 'text/event-stream', body })
    }
    return reply({}, 404)
  })
}

test('question tabs submit answers, hide generic tool cards and restore answer history', async ({ page }) => {
  await mockQuestions(page)
  await page.goto('/')
  await page.getByRole('button', { name: '以游客登录' }).click()
  await expect(page.getByLabel('回答问题', { exact: true })).toBeVisible()
  await expect(page.getByLabel('工具调用', { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: 'PostgreSQL', exact: true }).click()
  await expect(page.getByText('已回答 1/2 · 第 1/2 页')).toBeVisible()
  await page.screenshot({ path: 'test-results/questions-tabs.png', fullPage: true })
  await page.getByRole('button', { name: '下一题' }).click()
  await page.getByRole('button', { name: '提交', exact: true }).click()
  await expect(page.getByLabel('回答问题', { exact: true })).toHaveCount(0)
  await expect(page.getByLabel('问答记录')).toContainText('PostgreSQL')
  await expect(page.getByLabel('问答记录')).toContainText('用户未回答')
  await expect(page.getByLabel('助手消息', { exact: true })).toContainText('已收到你的回答。')
  await page.reload()
  await expect(page.getByLabel('问答记录')).toHaveCount(1)
  await expect(page.getByLabel('问答记录')).toContainText('PostgreSQL')
  await expect(page.getByLabel('工具调用', { exact: true })).toHaveCount(0)
  await page.screenshot({ path: 'test-results/questions-history.png', fullPage: true })
})

test.describe('mobile questions', () => {
  test.use({ viewport: { width: 360, height: 640 }, hasTouch: true, isMobile: true })
  test('long questions scroll independently while answers and stop remain accessible', async ({ page }) => {
    await mockQuestions(page, [
      { question: '请结合实际场景说明你的需求。'.repeat(30), choice: ['方案一', '方案二', '方案三'] },
      { question: '补充说明？', choice: [] },
    ])
    await page.goto('/')
    await page.getByRole('button', { name: '以游客登录' }).click()
    await expect(page.getByLabel('回答问题', { exact: true })).toBeVisible()
    await expect(page.getByRole('textbox', { name: '消息输入', exact: true })).not.toBeVisible()
    for (const viewport of [{ width: 360, height: 640 }, { width: 320, height: 568 }, { width: 360, height: 360 }, { width: 844, height: 390 }]) {
      await page.setViewportSize(viewport)
      await expect(page.getByRole('button', { name: '下一题', exact: true })).toBeInViewport({ ratio: 1 })
      await expect(page.getByRole('button', { name: '停止任务' })).toBeInViewport({ ratio: 1 })
      await expect(page.getByRole('textbox', { name: '自填答案' })).toBeInViewport({ ratio: 1 })
      expect((await page.getByLabel('消息列表', { exact: true }).boundingBox())!.height).toBeGreaterThan(30)
      expect(await page.getByRole('tabpanel').evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true)
    }
    await page.setViewportSize({ width: 360, height: 640 })
    await page.getByRole('textbox', { name: '自填答案' }).fill('我的手机回复')
    await page.getByRole('button', { name: '下一题', exact: true }).click()
    await page.screenshot({ path: 'test-results/mobile-question.png' })
    await page.getByRole('button', { name: '提交', exact: true }).click()
    await expect(page.getByLabel('问答记录')).toContainText('我的手机回复')
    await expect(page.getByRole('textbox', { name: '消息输入', exact: true })).toBeVisible()
  })

  test('stop cancels instead of submitting skipped answers', async ({ page }) => {
    await mockQuestions(page)
    const posts: string[] = []
    page.on('request', request => { if (request.method() === 'POST') posts.push(new URL(request.url()).pathname) })
    await page.goto('/')
    await page.getByRole('button', { name: '以游客登录' }).click()
    await page.getByRole('button', { name: '停止任务' }).click()
    await expect(page.getByRole('button', { name: '继续', exact: true })).toBeVisible()
    expect(posts).toContain('/api/v1/runs/1/cancel')
    expect(posts).not.toContain('/api/v1/runs/1/answers')
  })
})
