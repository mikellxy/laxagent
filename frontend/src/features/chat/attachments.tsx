import { useEffect, useRef, useState } from 'react'
import { Download, FileText, ImageIcon, LoaderCircle, Plus, RotateCcw, X } from 'lucide-react'
import { api } from '../../api/resources'
import { messageOf } from '../../api/errors'
import type { AppendixDisplay } from '../../api/types'
import styles from './Attachments.module.css'

export const IMAGE_EXTENSIONS = ['png', 'jpg', 'jpeg', 'webp', 'gif']
export const DOCUMENT_EXTENSIONS = ['pdf', 'txt', 'md', 'csv', 'docx', 'xlsx', 'pptx']
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

export function DraftAttachments({ items, remove, retry }: { items: DraftAppendix[]; remove: (keys: number[]) => void; retry: (key: number) => void }) {
  return <div className={styles.list} aria-label="待发送附件">{items.map(item => <div key={item.key} className={`${styles.card} ${item.kind === 'image' ? styles.imageCard : ''}`}>
    {item.url ? <img className={styles.thumbnail} src={item.url} alt={item.file.name} /> : <FileText className={styles.fileIcon} size={26} />}
    <div className={styles.label}><span title={item.file.name}>{item.file.name}</span><small role="status" title={item.error}>
      {item.status === 'uploading' ? '上传中…' : item.status === 'failed' ? item.error : '已上传'}
    </small></div>
    {item.status === 'uploading' && <LoaderCircle size={15} className="spin" />}
    {item.status === 'failed' && <button aria-label={`重试 ${item.file.name}`} onClick={() => retry(item.key)}><RotateCcw size={15} /></button>}
    <button className={styles.remove} aria-label={`移除 ${item.file.name}`} onClick={() => remove([item.key])}><X size={14} /></button>
  </div>)}</div>
}

function download(url: string, name: string) {
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = name
  document.body.append(anchor); anchor.click(); anchor.remove()
}
function ImagePreview({ url, name, close }: { url: string; name: string; close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => { dialog.current?.showModal(); return () => URL.revokeObjectURL(url) }, [url])
  return <dialog ref={dialog} className={styles.preview} aria-label={`预览 ${name}`} onClose={close}
    onClick={event => { if (event.target === event.currentTarget) close() }}>
    <div className={styles.previewToolbar}><span title={name}>{name}</span>
      <button aria-label={`下载 ${name}`} onClick={() => download(url, name)}><Download size={18} /></button>
      <button aria-label="关闭图片预览" onClick={close}><X size={20} /></button></div>
    <img src={url} alt={name} />
  </dialog>
}
export function MessageAttachments({ items = [] }: { items?: AppendixDisplay[] }) {
  const [preview, setPreview] = useState<{ url: string; name: string } | null>(null)
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
      if (abort.signal.aborted) return
      const url = URL.createObjectURL(blob)
      if (item.kind === 'image') setPreview({ url, name: item.name })
      else { download(url, item.name); setTimeout(() => URL.revokeObjectURL(url), 1000) }
    } catch (error) { if (!abort.signal.aborted) setError(messageOf(error)) }
    finally { if (!abort.signal.aborted) setLoading(null) }
  }
  if (!items.length) return null
  return <>
    <div className={styles.list} aria-label="消息附件">{items.map(item => <button key={item.id} title={item.name}
      className={`${styles.card} ${item.kind === 'image' ? styles.imageCard : ''}`} onClick={() => void open(item)} aria-label={`${item.kind === 'image' ? '查看' : '下载'} ${item.name}`}>
      {item.kind === 'image' ? (item.thumbnail_data_url ? <img className={styles.thumbnail} src={item.thumbnail_data_url} alt={item.name} /> : <ImageIcon size={26} />) : <FileText className={styles.fileIcon} size={26} />}
      <span className={styles.label}>{item.name}</span>{loading === item.id && <LoaderCircle size={15} className="spin" />}
    </button>)}</div>
    {error && <div className="error" role="alert">{error}</div>}
    {preview && <ImagePreview {...preview} close={() => setPreview(null)} />}
  </>
}
