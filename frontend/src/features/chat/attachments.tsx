import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Download, FileText, FileCode, FileSpreadsheet, FileType, Presentation, ImageIcon, LoaderCircle, Plus, RotateCcw, X } from 'lucide-react'
import { api } from '../../api/resources'
import { messageOf } from '../../api/errors'
import type { AppendixDisplay } from '../../api/types'
import styles from './Attachments.module.css'
import previewStyles from './PreviewWindow.module.css'
import { attachmentText, extensionOf } from './attachment-content'

export const IMAGE_EXTENSIONS = ['png', 'jpg', 'jpeg', 'webp', 'gif']
export const DOCUMENT_EXTENSIONS = ['pdf', 'txt', 'md', 'csv', 'docx', 'xlsx', 'pptx', 'html', 'htm']
const MiB = 1024 * 1024
export interface DraftAppendix {
  key: number; file: File; kind: 'image' | 'file'; url: string | null
  status: 'uploading' | 'ready' | 'failed'; error?: string; value?: AppendixDisplay; abort: AbortController
}

export function useAppendices() {
  const [items, setItems] = useState<DraftAppendix[]>([])
  const [error, setError] = useState<string | null>(null)
  const current = useRef<DraftAppendix[]>([])
  const sequence = useRef(0)
  const alive = useRef(true)
  const retired = useRef(new Set<string>())
  const update = (value: DraftAppendix[]) => { current.current = value; if (alive.current) setItems(value) }
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      for (const item of current.current) { item.abort.abort(); if (item.url) URL.revokeObjectURL(item.url) }
      retired.current.forEach(url => URL.revokeObjectURL(url)); retired.current.clear()
    }
  }, [])
  async function upload(item: DraftAppendix) {
    try {
      const result = await api.uploadAppendix(item.file, item.abort.signal)
      if (!alive.current || item.abort.signal.aborted) return
      update(current.current.map(row => row.key === item.key ? { ...row, status: 'ready',
        value: { ...result, kind: item.kind, thumbnail_data_url: item.url } } : row))
    } catch (error) {
      if (!alive.current || item.abort.signal.aborted) return
      update(current.current.map(row => row.key === item.key ? { ...row, status: 'failed', error: messageOf(error) } : row))
    }
  }
  function add(files: File[], kind: 'image' | 'file') {
    const errors: string[] = [], added: DraftAppendix[] = []
    let count = current.current.length, total = current.current.reduce((sum, item) => sum + item.file.size, 0)
    for (const file of files) {
      const ext = file.name.split('.').pop()?.toLowerCase() ?? ''
      if (!(kind === 'image' ? IMAGE_EXTENSIONS : DOCUMENT_EXTENSIONS).includes(ext)) { errors.push(`${file.name}：不支持的文件类型`); continue }
      if (file.size > 10 * MiB) { errors.push(`${file.name}：单个附件不能超过 10 MiB`); continue }
      if (!file.size) { errors.push(`${file.name}：不能上传空文件`); continue }
      if (count >= 5 || total + file.size > 20 * MiB) { errors.push('最多添加 5 个附件，合计不能超过 20 MiB'); continue }
      const item: DraftAppendix = { key: ++sequence.current, file, kind, url: kind === 'image' ? URL.createObjectURL(file) : null,
        status: 'uploading', abort: new AbortController() }
      count++; total += file.size; added.push(item)
    }
    setError(errors.length ? [...new Set(errors)].join('；') : null)
    update([...current.current, ...added])
    added.forEach(item => void upload(item))
  }
  function remove(keys: number[]) {
    for (const item of current.current.filter(item => keys.includes(item.key))) {
      item.abort.abort()
      // Optimistic messages may still display a submitted preview until history catches up.
      if (item.url) retired.current.add(item.url)
    }
    update(current.current.filter(item => !keys.includes(item.key)))
  }
  function retry(key: number) {
    const old = current.current.find(item => item.key === key)
    if (!old || old.status !== 'failed') return
    const item: DraftAppendix = { ...old, status: 'uploading', error: undefined, abort: new AbortController() }
    update(current.current.map(row => row.key === key ? item : row)); void upload(item)
  }
  function releaseRetired() { retired.current.forEach(url => URL.revokeObjectURL(url)); retired.current.clear() }
  return { items, error, add, remove, retry, releaseRetired }
}

export function UploadMenu({ disabled, add }: { disabled: boolean; add: (files: File[], kind: 'image' | 'file') => void }) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null), images = useRef<HTMLInputElement>(null), files = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (disabled) setOpen(false)
  }, [disabled])
  useEffect(() => {
    if (!open) return
    const close = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false) }
    document.addEventListener('pointerdown', close)
    return () => document.removeEventListener('pointerdown', close)
  }, [open])
  return <div className={styles.upload} ref={root} onKeyDown={event => { if (event.key === 'Escape') setOpen(false) }}>
    <button type="button" className={styles.plus} aria-label="添加附件" aria-expanded={open} aria-haspopup="menu"
      disabled={disabled} title={disabled ? '当前模型不支持附件' : '添加附件'} onClick={() => setOpen(!open)}><Plus size={23} /></button>
    {open && <div role="menu" aria-label="上传附件" className={styles.menu}>
      <button role="menuitem" onClick={() => { setOpen(false); files.current?.click() }}><FileText size={17} />上传文件</button>
      <button role="menuitem" onClick={() => { setOpen(false); images.current?.click() }}><ImageIcon size={17} />上传图片</button>
    </div>}
    <input hidden ref={files} type="file" multiple aria-label="选择文件" accept={DOCUMENT_EXTENSIONS.map(ext => `.${ext}`).join(',')}
      onChange={event => { if (!disabled) add(Array.from(event.target.files ?? []), 'file'); event.target.value = '' }} />
    <input hidden ref={images} type="file" multiple aria-label="选择图片" accept={IMAGE_EXTENSIONS.map(ext => `.${ext}`).join(',')}
      onChange={event => { if (!disabled) add(Array.from(event.target.files ?? []), 'image'); event.target.value = '' }} />
  </div>
}

function FileTile({ name }: { name: string }) {
  const extension = extensionOf(name)
  const format = extension === 'pdf' ? 'pdf' : ['xlsx', 'csv'].includes(extension) ? 'sheet'
    : ['html', 'htm'].includes(extension) ? 'code' : extension === 'pptx' ? 'slides' : 'document'
  const Icon = { pdf: FileType, sheet: FileSpreadsheet, code: FileCode, slides: Presentation, document: FileText }[format]
  return <><span className={styles.filename} title={name}>{name}</span>
    <span className={`${styles.fileIcon} ${styles[format]}`} aria-hidden="true"><Icon size={23} strokeWidth={1.5} /><small>{extension.toUpperCase()}</small></span></>
}

interface PreviewFile { blob: Blob; name: string; kind: 'image' | 'file' }

export function DraftAttachments({ items, remove, retry }: { items: DraftAppendix[]; remove: (keys: number[]) => void; retry: (key: number) => void }) {
  const [selected, setSelected] = useState<number | null>(null)
  const preview = items.find(item => item.key === selected)
  return <>
    <div className={`${styles.list} ${styles.drafts}`} aria-label="待发送附件">{items.map(item => <div key={item.key} className={styles.draft}>
      <button type="button" className={`${styles.card} ${item.kind === 'image' ? styles.imageCard : ''}`}
        aria-label={`查看 ${item.file.name}`} onClick={() => setSelected(item.key)}>
        {item.url ? <img className={styles.thumbnail} src={item.url} alt={item.file.name} /> : <FileTile name={item.file.name} />}
      </button>
      <span role="status" className={item.status === 'ready' ? styles.srOnly : styles.status} title={item.error}>
        {item.status === 'uploading' ? <><LoaderCircle size={14} className="spin" /><span className={styles.srOnly}>上传中…</span></>
          : item.status === 'failed' ? <span className={styles.srOnly}>{item.error}</span> : '已上传'}
      </span>
      {item.status === 'failed' && <button type="button" className={styles.retry} title={item.error}
        aria-label={`重试 ${item.file.name}`} onClick={() => retry(item.key)}><RotateCcw size={14} />上传失败，重试</button>}
      <button type="button" className={styles.remove} aria-label={`移除 ${item.file.name}`} onClick={() => remove([item.key])}><X size={13} /></button>
    </div>)}</div>
    {preview && <AttachmentPreview blob={preview.file} name={preview.file.name} kind={preview.kind} close={() => setSelected(null)} />}
  </>
}

function download(url: string, name: string) {
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = name
  document.body.append(anchor); anchor.click(); anchor.remove()
}

function AttachmentPreview({ blob, name, kind, close }: PreviewFile & { close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [url, setUrl] = useState('')
  const [text, setText] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    dialog.current?.showModal()
    return () => opener?.focus()
  }, [])
  useEffect(() => {
    // Each dialog owns its URL; closing it must not revoke the draft thumbnail.
    const objectUrl = URL.createObjectURL(blob), abort = new AbortController()
    setUrl(objectUrl); setText(null); setError(null)
    if (kind === 'file') void attachmentText(blob, name, abort.signal).then(value => {
      if (!abort.signal.aborted) setText(value)
    }).catch(() => {
      if (!abort.signal.aborted) setError('无法预览此文件的内容，请下载查看原件。')
    })
    return () => { abort.abort(); URL.revokeObjectURL(objectUrl) }
  }, [blob, name, kind])
  return createPortal(<dialog ref={dialog} className={`${previewStyles.dialog} ${kind === 'image' ? previewStyles.imageDialog : ''}`}
    aria-label={`预览 ${name}`} onCancel={event => { event.preventDefault(); close() }} onClose={close}>
    <div className={previewStyles.toolbar}>
      {kind === 'file' && <span className={previewStyles.title} title={name}>{name}</span>}
      <button type="button" disabled={!url} aria-label={`下载 ${name}`} title="下载" onClick={() => download(url, name)}><Download size={19} /></button>
      <button type="button" aria-label={kind === 'image' ? '关闭图片预览' : '关闭文件预览'} title="关闭" onClick={close}><X size={21} /></button>
    </div>
    {kind === 'image' ? <img src={url || undefined} alt={name} />
      : error ? <div className={previewStyles.state} role="alert">{error}</div>
      : text === null ? <div className={previewStyles.state} role="status"><LoaderCircle size={18} className="spin" />正在读取文件内容…</div>
      : <textarea className={previewStyles.text} readOnly aria-label={`${name} 文件内容`} value={text} />}
  </dialog>, document.body)
}

export function MessageAttachments({ items = [] }: { items?: AppendixDisplay[] }) {
  const [preview, setPreview] = useState<PreviewFile | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState<number | null>(null)
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => request.current?.abort(), [])
  async function open(item: AppendixDisplay) {
    request.current?.abort()
    const abort = new AbortController(); request.current = abort
    setLoading(item.id); setError(null)
    try {
      const blob = await api.appendix(item.id, abort.signal)
      if (!abort.signal.aborted) setPreview({ blob, name: item.name, kind: item.kind })
    } catch (error) { if (!abort.signal.aborted) setError(messageOf(error)) }
    finally { if (!abort.signal.aborted) setLoading(null) }
  }
  if (!items.length) return null
  return <>
    <div className={styles.list} aria-label="消息附件">{items.map(item => <button type="button" key={item.id}
      className={`${styles.card} ${item.kind === 'image' ? styles.imageCard : ''}`} onClick={() => void open(item)} aria-label={`查看 ${item.name}`}>
      {item.kind === 'image' ? (item.thumbnail_data_url ? <img className={styles.thumbnail} src={item.thumbnail_data_url} alt={item.name} /> : <ImageIcon size={32} />) : <FileTile name={item.name} />}
      {loading === item.id && <span className={styles.status} role="status" aria-label="正在读取附件"><LoaderCircle size={15} className="spin" /></span>}
    </button>)}</div>
    {error && <div className="error" role="alert">{error}</div>}
    {preview && <AttachmentPreview {...preview} close={() => setPreview(null)} />}
  </>
}
