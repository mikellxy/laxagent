import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { QueryClient, useQuery } from '@tanstack/react-query'
import { Check, Copy, Download, Expand, RefreshCw, X } from 'lucide-react'
import Markdown, { defaultUrlTransform, type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../../api/resources'
import { messageOf } from '../../api/errors'
import type { WorkspaceFilePreview } from '../../api/types'
import styles from './WorkspaceFiles.module.css'
import previewStyles from './PreviewWindow.module.css'
import { htmlThumbnail } from './htmlThumbnail'
import { attachmentText, extensionOf } from './attachment-content'

// Decode only the URI path, once. URLSearchParams subsequently transports that literal path.
export function workspacePath(href: string | undefined): string | null {
  if (!href?.startsWith('workspace:/') || href.startsWith('workspace://')) return null
  const encoded = href.slice('workspace:/'.length)
  if (!encoded || /[?#\s]/.test(encoded)) return null
  try {
    const path = decodeURIComponent(encoded)
    if (/[\x00-\x1f\x7f\\]/.test(path) || path.startsWith('/') || path.endsWith('/') ||
        path.split('/').some(part => part === '..' || part === '.' || !part)) return null
    return path
  } catch { return null }
}

const FilesContext = createContext<{ sessionId: number; client: QueryClient } | null>(null)
export function WorkspaceFilesProvider({ sessionId, children }: { sessionId: number; children: ReactNode }) {
  const client = useMemo(() => new QueryClient({ defaultOptions: { queries: {
    retry: false, staleTime: Infinity, refetchOnWindowFocus: false, refetchOnReconnect: false,
  } } }), [sessionId])
  useEffect(() => () => { void client.cancelQueries(); client.clear() }, [client])
  return <FilesContext.Provider value={{ sessionId, client }}>{children}</FilesContext.Provider>
}

function useObjectUrl(blob?: Blob) {
  const [value, setValue] = useState<{ blob: Blob; url: string }>()
  useEffect(() => {
    if (!blob) return
    const url = URL.createObjectURL(blob)
    setValue({ blob, url })
    return () => URL.revokeObjectURL(url)
  }, [blob])
  return value?.blob === blob ? value?.url : undefined
}
function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url; anchor.download = name; anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
const canExtractDocument = (info: WorkspaceFilePreview) => info.kind === 'download' &&
  info.size <= 10 * 1024 * 1024 && ['pdf', 'docx', 'xlsx', 'pptx'].includes(extensionOf(info.name))
const canOpen = (info: WorkspaceFilePreview) => info.can_expand || canExtractDocument(info)
interface Preview { info: WorkspaceFilePreview; image?: Blob; html?: string; documentText?: string }
interface FullPreview extends Preview { text?: string }

function HtmlThumbnail({ source, name, open, busy }: { source: string; name: string; open: () => void; busy: boolean }) {
  const document = useMemo(() => htmlThumbnail(source), [source])
  const container = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(600)
  useEffect(() => {
    const element = container.current!
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(entries => setWidth(entries[0].contentRect.width))
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  return <div ref={container} className={styles.htmlThumbnail}>
      <iframe title={`${name} 静态缩略预览`} srcDoc={document} sandbox="" referrerPolicy="no-referrer" tabIndex={-1} aria-hidden="true" style={{ transform: `scale(${width / 960})` }} />
      <button type="button" aria-label={`查看 ${name} 源码`} disabled={busy} onClick={open} />
    </div>
}

function FileDialog({ value, close, download }: { value: FullPreview; close: () => void; download: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const url = useObjectUrl(value.image)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    dialog.current?.showModal()
    return () => opener?.focus()
  }, [])
  async function copy() {
    try { await navigator.clipboard.writeText(value.text!); setCopied(true) }
    catch { setError('复制失败，请重试') }
  }
  return createPortal(<dialog ref={dialog} className={`${previewStyles.dialog} ${value.image ? previewStyles.imageDialog : ''}`} aria-label={`预览 ${value.info.name}`} onCancel={event => { event.preventDefault(); close() }} onClose={close}>
    <header className={previewStyles.toolbar}>
      {!value.image && <span className={previewStyles.title} title={value.info.path}>{value.info.path}</span>}
      {value.text !== undefined && <button type="button" aria-label={copied ? '已复制完整内容' : '复制完整内容'} title={copied ? '已复制' : '复制'} onClick={() => void copy()}>{copied ? <Check /> : <Copy />}</button>}
      <button type="button" aria-label={`下载 ${value.info.name}`} title="下载" onClick={download}><Download /></button>
      <button type="button" aria-label="关闭文件预览" title="关闭" onClick={close}><X /></button>
    </header>
    {error && <p role="alert">{error}</p>}
    {value.text !== undefined ? <pre className={previewStyles.text} tabIndex={0}><code>{value.text}</code></pre> : url ? <img src={url} alt={value.info.name} /> :
      <p className={previewStyles.state}>{value.info.can_expand ? '正在加载…' : '此文件不支持完整预览，请下载查看。'}</p>}
  </dialog>, document.body)
}

function FileReference({ path, children, block, complete }: { path: string; children: ReactNode; block: boolean; complete: boolean }) {
  const context = useContext(FilesContext)!
  const { sessionId, client } = context
  const key = ['workspace-file', path]
  const load = async (signal: AbortSignal): Promise<Preview> => {
    const info = await api.workspacePreview(sessionId, path, signal)
    const image = info.kind === 'image' ? await api.workspaceContent(sessionId, path, false, signal) : undefined
    let html: string | undefined
    if (info.kind === 'text' && info.language === 'html' && info.can_expand) {
      const blob = await api.workspaceContent(sessionId, path, false, signal)
      if (!blob.type.startsWith('text/plain')) throw new Error('文件类型已变化，请刷新后重试')
      html = await blob.text()
    }
    let documentText: string | undefined
    if (canExtractDocument(info)) {
      try {
        const blob = await api.workspaceContent(sessionId, path, true, signal)
        if (blob.size > 10 * 1024 * 1024) documentText = '文件超过文本提取上限，请下载查看原件。'
        else documentText = await attachmentText(blob, info.name, signal)
      } catch (error) {
        signal.throwIfAborted()
        documentText = '无法提取此文件的文本，请下载查看原件。'
      }
    }
    return { info, image, html, documentText }
  }
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => load(signal), enabled: complete && block,
    refetchOnMount: 'always' }, client)
  const [modal, setModal] = useState<FullPreview>()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const pending = useRef<AbortController | null>(null)
  useEffect(() => () => pending.current?.abort(), [])
  const url = useObjectUrl(complete ? query.data?.image : undefined)
  const info = complete ? query.data?.info : undefined
  async function action(kind: 'refresh' | 'open' | 'download' | 'copy') {
    if (!complete || busy) return
    const controller = new AbortController()
    pending.current = controller
    setBusy(true); setError(''); setCopied(false)
    try {
      if (kind === 'download') {
        const blob = await api.workspaceContent(sessionId, path, true, controller.signal)
        if (!controller.signal.aborted) saveBlob(blob, path.split('/').at(-1)!)
      } else {
        // Explicit actions always read current content; ordinary rerenders use the session cache.
        const current = await client.fetchQuery({ queryKey: key, queryFn: ({ signal }) => load(signal), staleTime: 0 })
        if (controller.signal.aborted) return
        if (kind !== 'refresh') {
          const value: FullPreview = { ...current }
          if (current.documentText !== undefined) value.text = current.documentText
          else if (current.html !== undefined) value.text = current.html
          else if (current.info.kind === 'text' && current.info.can_expand) {
            const blob = await api.workspaceContent(sessionId, path, false, controller.signal)
            if (!blob.type.startsWith('text/plain')) throw new Error('文件类型已变化，请刷新后重试')
            value.text = await blob.text()
          }
          if (controller.signal.aborted) return
          if (kind === 'copy') {
            if (value.text === undefined) throw new Error('文件超过完整文本预览上限，请下载查看')
            await navigator.clipboard.writeText(value.text); setCopied(true)
          } else setModal(value)
        }
      }
    } catch (reason) { if (!controller.signal.aborted) setError(messageOf(reason)) }
    finally { if (!controller.signal.aborted) setBusy(false) }
  }
  const close = () => { pending.current?.abort(); setBusy(false); setModal(undefined) }
  const visual = !!url || query.data?.html !== undefined
  const snippet = query.data?.documentText ?? info?.text
  const body = <>
    {block && <>
      {!visual && <header className={previewStyles.toolbar}>
        <span className={previewStyles.title} title={path}>{path}</span>
        {complete && <>
          {info && canOpen(info) && (info.kind === 'text' || canExtractDocument(info)) && <button type="button" disabled={busy} title={copied ? '已复制' : '复制'} aria-label={`复制 ${path}`} onClick={() => void action('copy')}>{copied ? <Check /> : <Copy />}</button>}
          <button type="button" disabled={busy} title="刷新" aria-label={`刷新 ${path}`} onClick={() => void action('refresh')}><RefreshCw /></button>
          <button type="button" disabled={busy} title="下载" aria-label={`下载 ${path}`} onClick={() => void action('download')}><Download /></button>
          {info && canOpen(info) && <button type="button" disabled={busy} title="展开" aria-label={`展开 ${path}`} onClick={() => void action('open')}><Expand /></button>}
        </>}
      </header>}
      {!complete ? <div className={styles.placeholder}>回答完成后加载文件</div> : query.isPending ? <div className={styles.placeholder}>正在加载文件…</div> : null}
      {visual ? <div className={styles.media}>
        {query.data?.html !== undefined ? <HtmlThumbnail source={query.data.html} name={path} busy={busy} open={() => void action('open')} /> :
          <button type="button" className={styles.image} disabled={busy} aria-label={`查看 ${path}`} onClick={() => void action('open')}><img src={url} alt={info?.name} onError={() => setError('图片加载失败，请重新打开或下载查看')} /></button>}
        <button type="button" className={styles.mediaDownload} disabled={busy} title="下载" aria-label={`下载 ${path}`} onClick={() => void action('download')}><Download size={15} /></button>
      </div> : snippet && <pre className={`${styles.snippet} ${info && canOpen(info) ? styles.expandable : ''}`} tabIndex={info && canOpen(info) ? 0 : undefined}
        role={info && canOpen(info) ? 'button' : undefined} aria-label={info && canOpen(info) ? `查看 ${path} 完整内容` : undefined}
        onClick={() => { if (info && canOpen(info)) void action('open') }}
        onKeyDown={event => { if (info && canOpen(info) && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); void action('open') } }}>
        <code>{snippet.split('\n').slice(0, 12).join('\n')}</code>
      </pre>}
      {info && !canOpen(info) && <button type="button" className={styles.footer} disabled={busy} onClick={() => void action('download')}>
        下载文件查看完整内容
      </button>}
    </>}
    {complete && (error || query.error) ? <span className={styles.error} role="alert">{error || messageOf(query.error)} <button type="button" disabled={busy} onClick={() => void action(block ? 'refresh' : 'open')}>重试</button></span> : null}
    {modal && <FileDialog value={modal} close={close} download={() => void action('download')} />}
  </>
  return block ? <section className={visual ? styles.visualCard : styles.card} aria-label={`文件 ${path}`} aria-busy={busy}>{body}</section> : <>
    <button type="button" className={styles.inline} title={path} disabled={!complete || busy} onClick={() => void action('open')}>{children}</button>{body}
  </>
}

export function AssistantMarkdown({ text, complete = false, files = false }: { text: string; complete?: boolean; files?: boolean }) {
  const components = useMemo<Components>(() => ({
    p: ({ node, children, ...props }) => {
      const nodes = node?.children.filter(child => child.type !== 'text' || child.value.trim())
      const single = nodes?.length === 1 ? nodes[0] : undefined
      const path = files && single?.type === 'element' && single.tagName === 'a' ? workspacePath(String(single.properties.href ?? '')) : null
      if (path !== null && single?.type === 'element') {
        const label = single.children.map(child => child.type === 'text' ? child.value : '').join('') || path
        return <FileReference path={path} complete={complete} block>{label}</FileReference>
      }
      return <p {...props}>{children}</p>
    },
    a: ({ children, node: _node, ...props }) => {
      const path = files ? workspacePath(props.href) : null
      if (path !== null) return <FileReference path={path} complete={complete} block={false}>{children}</FileReference>
      return <a {...props} target="_blank" rel="noopener noreferrer">{children}</a>
    },
  }), [files, complete])
  return <Markdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={(url, key, node) =>
    files && node.tagName === 'a' && key === 'href' && workspacePath(url) !== null ? url : defaultUrlTransform(url)
  } components={components}>{text}</Markdown>
}
