import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '../src/api/resources'
import { Chat } from '../src/features/chat/Chat'
import type { Message, Run, StreamEvent } from '../src/api/types'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

function setup(initial: Message[], status: Run['status'] = 'running', hasMore = false) {
  const state = { messages: initial, status, hasMore, resume_time: 0, error_desc: status === 'failed' ? '模型回答意外中断，请重试。' : null }
  let receive: (event: StreamEvent) => void = () => {}
  vi.spyOn(api, 'detail').mockImplementation(async () => ({ id: 1, user_id: 'owner', name: 'chat', project_id: 1,
    next_msg_seq: (state.messages.at(-1)?.seq ?? 0) + 1,
    latest_run: { run_id: 10, session_id: 1, status: state.status, model: 'p:m', error: null, error_code: null,
      error_desc: state.error_desc, resume_time: state.resume_time } }))
  vi.spyOn(api, 'messages').mockImplementation(async (_id, before) => ({
    items: state.messages.filter(message => before === undefined || message.seq < before), has_more: state.hasMore,
    next_before_seq: state.hasMore ? state.messages[0].seq : null,
  }))
  vi.spyOn(api, 'models').mockResolvedValue({ default_model: 'p:m', models: [{ id: 'p:m', provider: 'p', name: 'm' }] })
  vi.spyOn(api, 'stream').mockImplementation(async (_id, callback, signal) => {
    receive = callback
    await new Promise<void>(resolve => signal.addEventListener('abort', () => resolve()))
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><Chat owner="owner" sessionId={1} onMissing={() => {}} /></QueryClientProvider>)
  const event = async (type: string, seq: number | null, content: StreamEvent['content'] = '') => {
    await act(async () => receive({ run_id: 10, msg_seq: seq, type, content, role: 'assistant', error: null, channel: 'text' }))
  }
  return { state, event, user: userEvent.setup() }
}

it('shows partial tool arguments before execution and keeps the same disclosure open through completion', async () => {
  const h = setup([{ seq: 1, role: 'user', content: 'question' }])
  await waitFor(() => expect(api.stream).toHaveBeenCalled())
  await h.event('message_chunk', 2, '开始编写')
  await h.event('tool_call_start', 2, { tool_call_id: 'a', tool_name: 'write_file' })
  await h.user.click(screen.getByRole('button', { name: '正在工作' }))
  const card = screen.getByRole('button', { name: 'write_file · 正在生成参数' })
  await h.user.click(card)
  await h.event('tool_call_delta', 2, { tool_call_id: 'a', arguments_delta: '{"content":"第一段' })
  expect(screen.getByText('{"content":"第一段')).toBeVisible()
  await h.event('tool_call_delta', 2, { tool_call_id: 'a', arguments_delta: '第二段"}' })
  expect(screen.getByText(/第一段第二段/)).toBeVisible()
  await h.event('message_end', 2, { text: '开始编写' })
  await h.event('tool_start', 3, { tool_call_id: 'a', tool_name: 'write_file', arguments: '{"content":"最终内容"}' })
  expect(screen.getByRole('button', { name: 'write_file · 正在执行' })).toBe(card)
  expect(card).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getByText(/最终内容/)).toBeVisible()
  expect(screen.getAllByLabelText('工具调用')).toHaveLength(1)
  await h.event('tool_end', 3, { tool_call_id: 'a', error_code: null })
  expect(screen.getByRole('button', { name: 'write_file · 已完成' })).toBe(card)
})

it.each(['failed', 'cancelled'] as const)('shows the %s resume action and preserves the draft', async status => {
  const h = setup([{ seq: 1, role: 'user', content: 'question' }], status)
  h.state.error_desc = '模型回答意外中断，请重试。'
  const resume = vi.spyOn(api, 'resumeRun').mockImplementation(async () => {
    h.state.status = 'running'; h.state.resume_time = 1
    return { run_id: 10, session_id: 1, status: 'waiting', resume_time: 1 }
  })
  const action = await screen.findByRole('button', { name: status === 'failed' ? '重试' : '继续' })
  expect(screen.getByRole('status')).toHaveTextContent(status === 'failed' ? '模型回答意外中断，请重试。' : '已取消')
  await h.user.type(screen.getByRole('textbox', { name: '消息输入' }), '保留草稿')
  await h.user.click(action)
  await waitFor(() => expect(resume).toHaveBeenCalledExactlyOnceWith(10, 0, expect.any(AbortSignal)))
  expect(screen.getByRole('textbox', { name: '消息输入' })).toHaveValue('保留草稿')
  await waitFor(() => expect(screen.queryByRole('button', { name: status === 'failed' ? '重试' : '继续' })).toBeNull())
})

it('updates collapsed work previews only at line boundaries and hides them on expansion or completion', async () => {
  const h = setup([{ seq: 1, role: 'user', content: 'question' }])
  await waitFor(() => expect(api.stream).toHaveBeenCalled())
  await h.event('reasoning_chunk', 2, '第一行')
  const work = screen.getByRole('button', { name: '正在工作' })
  expect(work).toHaveTextContent(/^正在工作$/)
  await h.event('reasoning_chunk', 2, '\n第二行的一半')
  expect(work).toHaveTextContent('正在工作(第一行)')
  const preview = screen.getByTitle('第一行').firstChild
  await h.event('reasoning_chunk', 2, '剩下内容')
  expect(screen.getByTitle('第一行').firstChild).toBe(preview)
  await h.event('reasoning_chunk', 2, '\n')
  expect(work).toHaveTextContent('正在工作(第二行的一半剩下内容)')
  await h.user.click(work)
  expect(work).toHaveTextContent(/^正在工作$/)
  await h.event('reasoning_chunk', 2, '第三行\n')
  await h.user.click(work)
  expect(work).toHaveTextContent('正在工作(第三行)')
  await h.event('message_end', 2, { text: '' })
  await h.event('tool_start', 3, { tool_name: 'read_file', arguments: '{}' })
  expect(work).toHaveTextContent('正在工作(read_file)')
  await h.event('tool_end', 3, { result: 'never preview tool output' })
  expect(work).toHaveTextContent('正在工作(read_file)')
  await h.event('message_chunk', 4, '完成')
  expect(screen.getByRole('button', { name: '已工作' })).toHaveTextContent(/^已工作$/)
})

it('keeps both disclosures open through chunks, completion and durable reconciliation', async () => {
  const h = setup([{ seq: 1, role: 'user', content: 'question' }])
  await waitFor(() => expect(api.stream).toHaveBeenCalled())
  await h.event('reasoning_chunk', 2, '**第一步**')
  const work = screen.getByRole('button', { name: '正在工作' })
  expect(work).toHaveAttribute('aria-expanded', 'false')
  expect(screen.queryByText('第一步')).toBeNull()
  await h.user.click(work)
  const thinking = screen.getByRole('button', { name: '正在思考' })
  expect(thinking).toHaveAttribute('aria-expanded', 'false')
  await h.user.click(thinking)
  await h.event('reasoning_chunk', 2, '，第二步')
  expect(screen.getByText('第一步').tagName).toBe('STRONG')
  expect(screen.getByText('，第二步', { exact: false })).toBeVisible()
  await h.event('message_chunk', 2, '我先检查相关文件。')
  expect(screen.getByRole('button', { name: '已工作' })).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getByRole('button', { name: '已思考' })).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getAllByLabelText('工作过程').every(group => within(group).queryByText('我先检查相关文件。') === null)).toBe(true)
  const reasoning = [{ channel: 'text' as const, text: '**第一步**，第二步' }]
  await h.event('message_end', 2, { text: '我先检查相关文件。', reasoning })
  await h.event('tool_start', 3, { tool_name: 'read_file', arguments: '{"path":"hello.txt"}' })
  const next = screen.getByRole('button', { name: '正在工作' })
  expect(next).toHaveAttribute('aria-expanded', 'false')
  await h.user.click(next)
  await h.user.click(screen.getByRole('button', { name: 'read_file' }))
  expect(screen.getByText(/hello.txt/)).toBeVisible()
  await h.event('tool_end', 3, { result: 'hidden output' })
  expect(screen.queryByText('hidden output')).toBeNull()
  await h.event('reasoning_chunk', 4, '继续')
  await h.user.click(screen.getByRole('button', { name: '正在思考' }))
  await h.user.click(next)
  await h.event('reasoning_chunk', 4, '分析')
  await h.user.click(next)
  expect(screen.getByText('继续分析')).toBeVisible()
  h.state.messages.push({ seq: 2, role: 'assistant', content: '我先检查相关文件。', reasoning },
    { seq: 3, role: 'tool', tool_name: 'read_file', arguments: '{"path":"hello.txt"}' },
    { seq: 4, role: 'assistant', content: '完成', reasoning: [{ channel: 'text', text: '继续分析' }] })
  h.state.status = 'success'
  await h.event('sse_end', null)
  await waitFor(() => expect(screen.getByText('完成')).toBeVisible())
  expect(screen.getAllByRole('button', { name: '已工作' }).every(button => button.getAttribute('aria-expanded') === 'true')).toBe(true)
  expect(screen.getByText('继续分析')).toBeVisible()
  expect(screen.getByText(/hello.txt/)).toBeVisible()
})

it('retains expansion when an older page adds a boundary and merges a leading work group', async () => {
  const h = setup([{ seq: 4, role: 'assistant', content: '', reasoning: [{ channel: 'summary', text: '现有思考' }] },
    { seq: 5, role: 'assistant', content: 'answer' }], 'success', true)
  const work = await screen.findByRole('button', { name: '已工作' })
  await h.user.click(work)
  await h.user.click(screen.getByRole('button', { name: '已思考' }))
  const list = screen.getByLabelText('消息列表')
  Object.defineProperty(list, 'scrollHeight', { configurable: true, value: 1000 })
  Object.defineProperty(list, 'clientHeight', { configurable: true, value: 300 })
  h.state.messages.unshift({ seq: 1, role: 'assistant', content: '', reasoning: [{ channel: 'text', text: '更早思考' }] },
    { seq: 2, role: 'assistant', content: '我先读取文件。' },
    { seq: 3, role: 'tool', tool_name: 'read_file', arguments: '{}' })
  h.state.hasMore = false
  await act(async () => {
    list.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: -20 }))
    list.scrollTop = 0
    list.dispatchEvent(new Event('scroll', { bubbles: true }))
  })
  await waitFor(() => expect(screen.getByText('我先读取文件。')).toBeVisible())
  const groups = screen.getAllByRole('button', { name: '已工作' })
  expect(groups[0]).toHaveAttribute('aria-expanded', 'false')
  expect(groups[1]).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getByText('现有思考')).toBeVisible()
  expect(screen.getByRole('button', { name: 'read_file' })).toBeVisible()
})


it.each(['waiting', 'running'] as const)('shows working from %s status before the first SSE event', async status => {
  setup([{ seq: 1, role: 'user', content: '等待首 token' }], status)
  const work = await screen.findByRole('status', { name: '正在工作' })
  const user = screen.getByLabelText('用户消息')
  expect(user.compareDocumentPosition(work) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(screen.queryByRole('button', { name: '正在工作' })).toBeNull()
})

it('keeps working after assistant text while question arguments are hidden, then removes it on pause', async () => {
  const h = setup([{ seq: 1, role: 'user', content: '提问' }, { seq: 2, role: 'assistant', content: '我先确认几个问题。' }])
  const work = await screen.findByRole('status', { name: '正在工作' })
  expect(screen.getByLabelText('助手消息').compareDocumentPosition(work) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  await h.event('tool_call_start', 2, { tool_call_id: 'q', tool_name: 'ask_user_question' })
  await h.event('tool_call_delta', 2, { tool_call_id: 'q', arguments_delta: '{"questions":[' })
  expect(screen.getAllByLabelText('工作过程')).toHaveLength(1)
  expect(screen.queryByText(/ask_user_question/)).toBeNull()
  expect(screen.getByRole('status', { name: '正在工作' })).toBeVisible()
  h.state.status = 'paused'
  await h.event('sse_end', null, { status: 'paused' })
  await waitFor(() => expect(screen.queryByRole('status', { name: '正在工作' })).toBeNull())
})

it('replaces the placeholder with one real work group and stops when status refresh reports completion', async () => {
  const h = setup([{ seq: 1, role: 'user', content: '检查' }])
  await screen.findByRole('status', { name: '正在工作' })
  await h.event('reasoning_chunk', 2, '检查中')
  expect(screen.queryByRole('status', { name: '正在工作' })).toBeNull()
  expect(screen.getByRole('button', { name: '正在工作' })).toBeVisible()
  expect(screen.getAllByLabelText('工作过程')).toHaveLength(1)
  h.state.status = 'success'
  h.state.messages.push({ seq: 2, role: 'assistant', content: '', reasoning: [{ channel: 'text', text: '检查中' }] })
  await act(async () => document.dispatchEvent(new Event('visibilitychange')))
  await screen.findByRole('button', { name: '已工作' })
  expect(screen.queryByRole('button', { name: '正在工作' })).toBeNull()
})

it.each(['paused', 'success', 'failed', 'cancelled'] as const)('does not show a working placeholder for %s', async status => {
  setup([{ seq: 1, role: 'user', content: 'question' }], status)
  await screen.findByLabelText('用户消息')
  expect(screen.queryByRole('status', { name: '正在工作' })).toBeNull()
})

it('shows working after an accepted user message before its durable row or SSE arrives', async () => {
  const h = setup([], 'success')
  vi.spyOn(api, 'chat').mockResolvedValue({ run_id: 11, session_id: 1, status: 'waiting', resume_time: 0 })
  await waitFor(() => expect(screen.getByRole('combobox', { name: '模型' })).toHaveValue('p:m'))
  await h.user.type(screen.getByRole('textbox', { name: '消息输入' }), '开始任务')
  await h.user.click(screen.getByRole('button', { name: '发送消息' }))
  const work = await screen.findByRole('status', { name: '正在工作' })
  const user = screen.getByLabelText('待确认用户消息')
  expect(user.compareDocumentPosition(work) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(screen.getAllByLabelText('工作过程')).toHaveLength(1)
})

it('fetches workspace references only at message_end, and reloads completed history without waiting for run end', async () => {
  const read = vi.spyOn(api, 'workspacePreview').mockResolvedValue({ path: 'code.py', name: 'code.py', size: 5,
    kind: 'text', language: 'python', text: 'hello', truncated: false, can_expand: true })
  const h = setup([{ seq: 1, role: 'user', content: 'question' }])
  await waitFor(() => expect(api.stream).toHaveBeenCalled())
  await h.event('message_chunk', 2, '[源码](workspace:/code')
  await h.event('message_chunk', 2, '.py)')
  expect(screen.getByText('回答完成后加载文件')).toBeVisible()
  expect(read).not.toHaveBeenCalled()
  await h.event('message_end', 2, { text: '[源码](workspace:/code.py)' })
  expect(await screen.findByText('hello')).toBeVisible()
  expect(read).toHaveBeenCalledTimes(1)
  await h.event('message_chunk', 3, '[未完成](workspace:/unfinished.py)')
  expect(read).toHaveBeenCalledTimes(1)
  await h.event('reasoning_chunk', 4, '[思考](workspace:/reasoning.py)')
  expect(read).toHaveBeenCalledTimes(1)
})
