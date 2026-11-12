import { expect, test } from '@playwright/test'

test('question tabs submit answers, hide generic tool cards and restore answer history', async ({ page }) => {
  const owner = '12345678-1234-4234-8234-123456789abc'
  const questions = [{ question: '使用哪种数据库？', choice: ['PostgreSQL', 'SQLite'] }, { question: '补充说明？', choice: [] }]
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
  await page.goto('/')
  await page.getByRole('button', { name: '以游客登录' }).click()
  await expect(page.getByLabel('回答问题', { exact: true })).toBeVisible()
  await expect(page.getByLabel('工具调用', { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: 'PostgreSQL', exact: true }).click()
  await expect(page.getByText('已回答 1/2 · 第 1/2 页')).toBeVisible()
  await page.screenshot({ path: 'test-results/questions-tabs.png', fullPage: true })
  await page.getByRole('button', { name: '下一题' }).click()
  await page.getByRole('button', { name: '提交答案' }).click()
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
