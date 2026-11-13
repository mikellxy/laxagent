import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../src/api/resources'
import { AssistantMarkdown, WorkspaceFilesProvider, workspacePath } from '../src/features/chat/WorkspaceFiles'
import type { WorkspaceFilePreview } from '../src/api/types'

const info = (path = 'hello.py', changes: Partial<WorkspaceFilePreview> = {}): WorkspaceFilePreview => ({
  path, name: path.split('/').at(-1)!, size: 9, kind: 'text', language: 'python', text: 'print(42)', truncated: false, can_expand: true, ...changes,
})
function blob(text: string, type = 'text/plain') {
  const value = new Blob([text], { type })
  Object.defineProperty(value, 'text', { value: async () => text })
  return value
}
function view(text: string, complete = true, files = true) {
  return <WorkspaceFilesProvider sessionId={1}><AssistantMarkdown text={text} complete={complete} files={files} /></WorkspaceFilesProvider>
}
beforeEach(() => {
  URL.createObjectURL = vi.fn(() => 'blob:preview')
  URL.revokeObjectURL = vi.fn()
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', '') }
  vi.spyOn(api, 'workspacePreview').mockImplementation(async (_session, path) => info(path))
  vi.spyOn(api, 'workspaceContent').mockResolvedValue(blob('full current file'))
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('decodes paths once and rejects traversal, foreign authorities and incomplete references', () => {
  expect(workspacePath('workspace:/中文%20%23%3F%25.py')).toBe('中文 #?%.py')
  expect(workspacePath('workspace:/%252e%252e/file')).toBe('%2e%2e/file')
  for (const url of ['workspace://host/a', 'workspace:/../a', 'workspace:/%2e%2e/a', 'workspace:/%2fa', 'workspace:/a#b',
    'workspace:/a?b', 'workspace:/a b', 'workspace:/a%', 'workspace:/%00a', 'workspace:/a%5cb', 'file:/a', '/a']) expect(workspacePath(url)).toBeNull()
})

it('waits for completion, uses final paths, deduplicates and keeps dialogs during text rerenders', async () => {
  const h = render(view('[旧](workspace:/old.py)', false))
  expect(screen.getByText('回答完成后加载文件')).toBeVisible()
  expect(api.workspacePreview).not.toHaveBeenCalled()
  h.rerender(view('[源码](workspace:/hello.py)\n\n[同一文件](workspace:/hello.py)'))
  await screen.findAllByText('print(42)')
  expect(api.workspacePreview).toHaveBeenCalledTimes(1)
  await userEvent.click(screen.getAllByRole('button', { name: '展开 hello.py' })[0])
  expect(await screen.findByRole('dialog')).toHaveTextContent('full current file')
  h.rerender(view('[源码](workspace:/hello.py)\n\n[同一文件](workspace:/hello.py)\n\n后续说明'))
  expect(screen.getByRole('dialog')).toHaveTextContent('full current file')
  await userEvent.click(screen.getByRole('button', { name: '关闭文件预览' }))
  expect(screen.queryByRole('dialog')).toBeNull()
})

it('opens inline links on click and never loads code examples or reasoning references', async () => {
  render(view('说明中的 [文件](workspace:/hello.py)，还有 [网站](https://example.com)。\n\n```md\n[代码示例](workspace:/code.py)\n```'))
  expect(api.workspacePreview).not.toHaveBeenCalled()
  expect(screen.getByRole('link', { name: '网站' })).toHaveAttribute('href', 'https://example.com')
  await userEvent.click(screen.getByRole('button', { name: '文件' }))
  await screen.findByRole('dialog')
  expect(api.workspacePreview).toHaveBeenCalledExactlyOnceWith(1, 'hello.py', expect.any(AbortSignal))
  cleanup(); vi.mocked(api.workspacePreview).mockClear()
  render(view('[思考](workspace:/reasoning.py)', true, false))
  expect(api.workspacePreview).not.toHaveBeenCalled()
})

it('renders source safely, reads fresh content on refresh, and downloads unsupported files', async () => {
  vi.mocked(api.workspacePreview).mockResolvedValueOnce(info('page.html', { text: '<script>alert(1)</script>', language: 'html', can_expand: false }))
    .mockResolvedValue(info('page.html', { text: 'updated' }))
  const h = render(view('[页面](workspace:/page.html)'))
  expect(await screen.findByText('<script>alert(1)</script>')).toBeVisible()
  expect(h.container.querySelector('script')).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: '刷新 page.html' }))
  expect(await screen.findByText('updated')).toBeVisible()
  vi.mocked(api.workspacePreview).mockResolvedValue(info('report.pdf', { kind: 'download', text: null, can_expand: false }))
  h.rerender(view('[报告](workspace:/report.pdf)'))
  await screen.findByText('下载文件查看完整内容')
  const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  await userEvent.click(screen.getByRole('button', { name: '下载 report.pdf' }))
  await waitFor(() => expect(download).toHaveBeenCalled())
  expect(api.workspaceContent).toHaveBeenLastCalledWith(1, 'report.pdf', true, expect.any(AbortSignal))
})

it('loads bounded full HTML only after completion, refreshes its thumbnail and opens unmodified source', async () => {
  const source = '<style>body { color: red }</style><h1>Page</h1><script>alert(1)</script>'
  vi.mocked(api.workspacePreview).mockResolvedValue(info('page.html', { text: '<style>', language: 'html', truncated: true }))
  vi.mocked(api.workspaceContent).mockResolvedValue(blob(source))
  const h = render(view('[页面](workspace:/page.html)', false))
  expect(api.workspaceContent).not.toHaveBeenCalled()
  h.rerender(view('[页面](workspace:/page.html)'))
  const frame = await screen.findByTitle('page.html 静态缩略预览')
  expect(frame).toHaveAttribute('sandbox', '')
  expect(frame.getAttribute('srcdoc')).toContain('<h1>Page</h1>')
  expect(frame.getAttribute('srcdoc')).not.toContain('<script>')
  await userEvent.click(screen.getByRole('button', { name: '查看 page.html 源码' }))
  expect(await screen.findByRole('dialog')).toHaveTextContent(source)
  await userEvent.click(screen.getByRole('button', { name: '关闭文件预览' }))
  vi.mocked(api.workspaceContent).mockResolvedValue(blob('<h1>Updated</h1>'))
  await userEvent.click(screen.getByRole('button', { name: '刷新 page.html' }))
  await waitFor(() => expect(frame.getAttribute('srcdoc')).toContain('<h1>Updated</h1>'))
})

it('does not load full HTML beyond the text preview limit', async () => {
  vi.mocked(api.workspacePreview).mockResolvedValue(info('large.html', { language: 'html', size: 3 * 1024 * 1024, can_expand: false, truncated: true }))
  render(view('[大页面](workspace:/large.html)'))
  await screen.findByText('print(42)')
  expect(api.workspaceContent).not.toHaveBeenCalled()
  expect(screen.queryByTitle('large.html 静态缩略预览')).toBeNull()
})

it('limits large text to a short preview and never copies truncated text', async () => {
  vi.mocked(api.workspacePreview).mockResolvedValue(info('big.txt', { size: 3 * 1024 * 1024, text: 'short preview', truncated: true, can_expand: false }))
  render(view('[大文件](workspace:/big.txt)'))
  await screen.findByText('short preview')
  expect(screen.queryByRole('button', { name: '复制 big.txt' })).toBeNull()
  expect(screen.queryByRole('button', { name: '展开 big.txt' })).toBeNull()
  expect(screen.getByText('下载文件查看完整内容')).toBeVisible()
})

it('displays images, releases object URLs, and retries a locally failed preview', async () => {
  vi.mocked(api.workspacePreview).mockRejectedValueOnce(new Error('文件不存在')).mockResolvedValue(info('image.png', { kind: 'image', text: null }))
  vi.mocked(api.workspaceContent).mockResolvedValue(blob('png', 'image/png'))
  const h = render(view('[图片](workspace:/image.png)'))
  expect(await screen.findByRole('alert')).toHaveTextContent('文件不存在')
  await userEvent.click(screen.getByRole('button', { name: '重试' }))
  expect(await screen.findByRole('img')).toHaveAttribute('src', 'blob:preview')
  await userEvent.click(screen.getByRole('button', { name: '查看 image.png' }))
  const dialog = await screen.findByRole('dialog')
  expect(await within(dialog).findByRole('img')).toBeVisible()
  fireEvent(dialog, new Event('cancel', { bubbles: false }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  h.unmount()
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:preview')
})

it('reads current content for a newly mounted reference while retaining deduplication', async () => {
  const h = render(<WorkspaceFilesProvider sessionId={1}><AssistantMarkdown text="[旧引用](workspace:/hello.py)" files complete /></WorkspaceFilesProvider>)
  await screen.findByText('print(42)')
  vi.mocked(api.workspacePreview).mockResolvedValue(info('hello.py', { text: 'new content' }))
  h.rerender(<WorkspaceFilesProvider sessionId={1}>
    <AssistantMarkdown text="[旧引用](workspace:/hello.py)" files complete />
    <AssistantMarkdown text="[新引用](workspace:/hello.py)" files complete />
  </WorkspaceFilesProvider>)
  await screen.findAllByText('new content')
  expect(api.workspacePreview).toHaveBeenCalledTimes(2)
})

it('keeps a streaming reference as a placeholder even when another message cached that file', async () => {
  const h = render(<WorkspaceFilesProvider sessionId={1}><AssistantMarkdown text="[已完成](workspace:/hello.py)" files complete /></WorkspaceFilesProvider>)
  await screen.findByText('print(42)')
  h.rerender(<WorkspaceFilesProvider sessionId={1}>
    <AssistantMarkdown text="[已完成](workspace:/hello.py)" files complete />
    <AssistantMarkdown text="[未完成](workspace:/hello.py)" files complete={false} />
  </WorkspaceFilesProvider>)
  const card = screen.getAllByRole('region', { name: '文件 hello.py' }).at(-1)!
  expect(card).toHaveTextContent('回答完成后加载文件')
  expect(card).not.toHaveTextContent('print(42)')
  expect(api.workspacePreview).toHaveBeenCalledTimes(1)
})
