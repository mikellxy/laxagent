import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { QueryClient, useQuery } from '@tanstack/react-query'
import { Copy, Download, Expand, FileText, RefreshCw, X } from 'lucide-react'
import Markdown, { defaultUrlTransform, type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../../api/resources'
import { messageOf } from '../../api/errors'
import type { WorkspaceFilePreview } from '../../api/types'
import styles from './WorkspaceFiles.module.css'

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
function sizeLabel(size: number) {
  return size < 1024 ? `${size} B` : size < 1024 * 1024 ? `${(size / 1024).toFixed(1)} KiB` : `${(size / 1024 / 1024).toFixed(1)} MiB`
}
interface Preview { info: WorkspaceFilePreview; image?: Blob }
interface FullPreview extends Preview { text?: string }

function FileDialog({ value, close, download }: { value: FullPreview; close: () => void; download: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const url = useObjectUrl(value.image)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { dialog.current?.showModal() }, [])
  async function copy() {
    try { await navigator.clipboard.writeText(value.text!); setCopied(true) }
    catch { setError('复制失败，请重试') }
  }
  return createPortal(<dialog ref={dialog} className={styles.dialog} aria-label={`预览 ${value.info.name}`} onCancel={close} onClose={close}>
    <header className={styles.header}>
      <span className={styles.path}>{value.info.path}</span>
      {value.text !== undefined && <button type="button" aria-label="复制完整内容" onClick={() => void copy()}><Copy size={16} />{copied ? '已复制' : ''}</button>}
      <button type="button" aria-label={`下载 ${value.info.name}`} onClick={download}><Download size={16} /></button>
      <button type="button" aria-label="关闭文件预览" onClick={close}><X size={18} /></button>
    </header>
    {error && <p role="alert">{error}</p>}
    <div className={styles.full}>
      {value.text !== undefined ? <pre><code>{value.text}</code></pre> : url ? <img src={url} alt={value.info.name} /> :
        <p>{value.info.can_expand ? '正在加载…' : '此文件不支持完整预览，请下载查看。'}</p>}
    </div>
  </dialog>, document.body)
}

function FileReference({ path, children, block, complete }: { path: string; children: ReactNode; block: boolean; complete: boolean }) {
  const context = useContext(FilesContext)!
  const { sessionId, client } = context
  const key = ['workspace-file', path]
  const load = async (signal: AbortSignal): Promise<Preview> => {
    const info = await api.workspacePreview(sessionId, path, signal)
    const image = info.kind === 'image' ? await api.workspaceContent(sessionId, path, false, signal) : undefined
    return { info, image }
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
          if (current.info.kind === 'text' && current.info.can_expand) {
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
  const body = <>
    {block && <>
      <header className={styles.header}>
        <FileText size={16} /><span className={styles.path} title={path}>{path}</span>
        {complete && <>
          {info?.kind === 'text' && info.can_expand && <button type="button" disabled={busy} aria-label={`复制 ${path}`} onClick={() => void action('copy')}><Copy size={15} />{copied ? '已复制' : ''}</button>}
          <button type="button" disabled={busy} aria-label={`刷新 ${path}`} onClick={() => void action('refresh')}><RefreshCw size={15} /></button>
          <button type="button" disabled={busy} aria-label={`下载 ${path}`} onClick={() => void action('download')}><Download size={15} /></button>
          {info?.can_expand && <button type="button" disabled={busy} aria-label={`展开 ${path}`} onClick={() => void action('open')}><Expand size={15} /></button>}
        </>}
      </header>
      <div className={styles.caption}>{children}{info && <span> · {info.language ?? info.kind} · {sizeLabel(info.size)}</span>}</div>
      {!complete ? <div className={styles.placeholder}>回答完成后加载文件</div> : query.isPending ? <div className={styles.placeholder}>正在加载文件…</div> : null}
      {info?.kind === 'text' && <pre className={styles.snippet}><code>{info.text?.split('\n').slice(0, 12).join('\n')}</code></pre>}
      {info?.kind === 'image' && url && <button type="button" className={styles.image} aria-label={`查看 ${path}`} onClick={() => void action('open')}><img src={url} alt={info.name} onError={() => setError('图片加载失败，请刷新或下载查看')} /></button>}
      {info && <button type="button" className={styles.footer} disabled={busy} onClick={() => void action(info.can_expand ? 'open' : 'download')}>
        {info.can_expand ? '查看完整内容' : '下载文件查看完整内容'}
      </button>}
    </>}
    {complete && (error || query.error) ? <span className={styles.error} role="alert">{error || messageOf(query.error)} <button type="button" disabled={busy} onClick={() => void action(block ? 'refresh' : 'open')}>重试</button></span> : null}
    {modal && <FileDialog value={modal} close={close} download={() => void action('download')} />}
  </>
  return block ? <section className={styles.card} aria-label={`文件 ${path}`} aria-busy={busy}>{body}</section> : <>
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
