import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../src/api/resources'
import { Chat } from '../src/features/chat/Chat'
import type { Message } from '../src/api/types'

beforeEach(() => {
  URL.createObjectURL = vi.fn(() => 'blob:test-preview')
  URL.revokeObjectURL = vi.fn()
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', '') }
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

function setup(messages: Message[] = [], hasAppendix = false) {
  vi.spyOn(api, 'detail').mockResolvedValue({ id: 1, user_id: 'attachment-owner', name: 'chat', project_id: 1,
    next_msg_seq: 5, latest_run: null, has_appendix: hasAppendix })
  vi.spyOn(api, 'messages').mockResolvedValue({ items: messages, has_more: false, next_before_seq: null })
  vi.spyOn(api, 'models').mockResolvedValue({ default_model: 'p:vision', models: [
    { id: 'p:vision', provider: 'p', name: 'vision', supports_multimodal: true },
    { id: 'p:text', provider: 'p', name: 'text', supports_multimodal: false },
  ] })
  vi.spyOn(api, 'stream').mockImplementation(async (_id, _callback, signal) => {
    await new Promise<void>(resolve => signal.addEventListener('abort', () => resolve()))
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(<QueryClientProvider client={client}><Chat owner="attachment-owner" sessionId={1} onMissing={() => {}} /></QueryClientProvider>)
  return { ...view, user: userEvent.setup() }
}
async function vision() {
  const model = await screen.findByRole('combobox', { name: '模型' })
  await waitFor(() => expect(model).not.toBeDisabled())
  fireEvent.change(model, { target: { value: 'p:vision' } })
  return model
}
function select(name = 'hello.txt', size = 5) {
  fireEvent.change(screen.getByLabelText('选择文件'), { target: { files: [new File(['a'.repeat(size)], name)] } })
}

it('uploads multiple files, rejects invalid selections and sends an attachment-only message', async () => {
  const h = setup()
  const upload = vi.spyOn(api, 'uploadAppendix').mockImplementation(async file => ({ id: file.name === 'a.txt' ? 1 : 2, name: file.name }))
  const chat = vi.spyOn(api, 'chat').mockResolvedValue({ run_id: 10, session_id: 1, status: 'waiting', resume_time: 0 })
  await vision()
  await h.user.click(screen.getByRole('button', { name: '添加附件' }))
  expect(screen.getByRole('menuitem', { name: '上传图片' })).toBeVisible()
  await h.user.click(screen.getByRole('menuitem', { name: '上传文件' }))
  fireEvent.change(screen.getByLabelText('选择文件'), { target: { files: [new File(['a'], 'a.txt'), new File(['b'], 'b.md'), new File(['zip'], 'no.zip')] } })
  await waitFor(() => expect(screen.getAllByText('已上传')).toHaveLength(2))
  expect(upload).toHaveBeenCalledTimes(2)
  expect(screen.getByRole('alert')).toHaveTextContent('不支持的文件类型')
  await h.user.click(screen.getByRole('button', { name: '发送消息' }))
  await waitFor(() => expect(chat).toHaveBeenCalled())
  expect(chat.mock.calls[0][1]).toBe('')
  expect(chat.mock.calls[0][4]?.map(item => item.id)).toEqual([1, 2])
  expect(screen.queryByLabelText('待发送附件')).toBeNull()
  expect(within(screen.getByLabelText('待确认用户消息')).getAllByRole('button')).toHaveLength(2)
})

it('blocks sending on failed uploads, retries, and retains files when switching models', async () => {
  const h = setup()
  vi.spyOn(api, 'uploadAppendix').mockRejectedValueOnce(new Error('上传中断')).mockResolvedValue({ id: 1, name: 'hello.txt' })
  const model = await vision()
  select()
  await screen.findByRole('button', { name: '重试 hello.txt' })
  expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  await h.user.click(screen.getByRole('button', { name: '重试 hello.txt' }))
  await screen.findByText('已上传')
  expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled()
  await h.user.selectOptions(model, 'p:text')
  expect(screen.getByRole('button', { name: '添加附件' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  expect(screen.getByText('当前模型不支持附件，请更换模型或移除附件')).toBeVisible()
  await h.user.click(screen.getByRole('button', { name: '移除 hello.txt' }))
  expect(screen.queryByLabelText('待发送附件')).toBeNull()
})

it('uses the full-history flag even when the loaded page contains no attachments', async () => {
  const h = setup([{ seq: 4, role: 'assistant', content: 'answer' }], true)
  const model = await vision()
  await h.user.type(screen.getByRole('textbox', { name: '消息输入' }), '继续')
  await h.user.selectOptions(model, 'p:text')
  expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  expect(screen.getByText('此会话包含附件，请选择支持多模态的模型，或新建会话')).toBeVisible()
})

it('reads originals only on click and releases image preview URLs', async () => {
  const h = setup([{ seq: 1, role: 'user', content: '', appendix: [
    { id: 1, name: 'picture.png', kind: 'image', thumbnail_data_url: 'data:image/webp;base64,eA==' },
    { id: 2, name: 'notes.txt', kind: 'file', thumbnail_data_url: null },
  ] }], true)
  const read = vi.spyOn(api, 'appendix').mockResolvedValue(new Blob(['image']))
  await screen.findByRole('button', { name: '查看 picture.png' })
  expect(read).not.toHaveBeenCalled()
  await h.user.click(screen.getByRole('button', { name: '查看 picture.png' }))
  expect(await screen.findByRole('dialog', { name: '预览 picture.png' })).toBeVisible()
  expect(read).toHaveBeenCalledWith(1, expect.any(AbortSignal))
  await h.user.click(screen.getByRole('button', { name: '关闭图片预览' }))
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:test-preview')
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  await h.user.click(screen.getByRole('button', { name: '下载 notes.txt' }))
  await waitFor(() => expect(click).toHaveBeenCalled())
})

it('aborts removed uploads and ignores late completions', async () => {
  const h = setup()
  let resolve!: (value: { id: number; name: string }) => void
  const upload = vi.spyOn(api, 'uploadAppendix').mockReturnValue(new Promise(done => { resolve = done }))
  await vision()
  select()
  expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  await h.user.click(screen.getByRole('button', { name: '移除 hello.txt' }))
  expect(upload.mock.calls[0][1]?.aborted).toBe(true)
  await act(async () => resolve({ id: 1, name: 'hello.txt' }))
  expect(screen.queryByLabelText('待发送附件')).toBeNull()
})

it('enforces count, individual size and aggregate size before uploading', async () => {
  setup()
  const upload = vi.spyOn(api, 'uploadAppendix').mockResolvedValue({ id: 1, name: 'file.txt' })
  await vision()
  const fake = (name: string, size: number) => { const file = new File(['x'], name); Object.defineProperty(file, 'size', { value: size }); return file }
  fireEvent.change(screen.getByLabelText('选择文件'), { target: { files: [fake('large.txt', 10 * 1024 * 1024 + 1)] } })
  expect(upload).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('选择文件'), { target: { files: [fake('one.txt', 10 * 1024 * 1024), fake('two.txt', 10 * 1024 * 1024), fake('three.txt', 1)] } })
  await waitFor(() => expect(upload).toHaveBeenCalledTimes(2))
  expect(screen.getByRole('alert')).toHaveTextContent('合计不能超过 20 MiB')
})

it('limits a message to five attachments and frees a slot when removed', async () => {
  const h = setup()
  const upload = vi.spyOn(api, 'uploadAppendix').mockImplementation(async file => ({ id: Number(file.name[0]), name: file.name }))
  await vision()
  const files = Array.from({ length: 6 }, (_, i) => new File(['x'], `${i + 1}.txt`))
  fireEvent.change(screen.getByLabelText('选择文件'), { target: { files } })
  await waitFor(() => expect(screen.getAllByText('已上传')).toHaveLength(5))
  expect(upload).toHaveBeenCalledTimes(5)
  await h.user.click(screen.getByRole('button', { name: '移除 1.txt' }))
  fireEvent.change(screen.getByLabelText('选择文件'), { target: { files: [files[5]] } })
  await waitFor(() => expect(screen.getAllByText('已上传')).toHaveLength(5))
  expect(upload).toHaveBeenCalledTimes(6)
})
