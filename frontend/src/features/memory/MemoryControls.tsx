import { useEffect, useId, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Trash2, X } from 'lucide-react'
import { api } from '../../api/resources'
import { messageOf } from '../../api/errors'
import type { MemoryItem } from '../../api/types'
import styles from './MemoryControls.module.css'

// A top-layer popover stays readable even in the last row of a scrolling drawer.
function FullText({ text, label }: { text: string; label: string }) {
  const id = useId()
  const preview = useRef<HTMLDivElement>(null)
  const anchor = useRef<HTMLButtonElement>(null)
  const pinned = useRef(false)
  function show() {
    const element = preview.current, box = anchor.current?.getBoundingClientRect()
    if (!element || !box) return
    const width = Math.min(440, window.innerWidth - 32)
    const height = Math.min(280, window.innerHeight - 32)
    element.style.width = `${width}px`
    element.style.maxHeight = `${height}px`
    element.style.left = `${Math.max(16, Math.min(box.left, window.innerWidth - width - 16))}px`
    element.style.top = `${Math.max(16, box.bottom + height < window.innerHeight - 16 ? box.bottom : box.top - height)}px`
    element.showPopover()
  }
  function hide() { if (!pinned.current) preview.current?.hidePopover() }
  return <span className={styles.cell} onMouseEnter={show} onMouseLeave={hide}>
    <button ref={anchor} type="button" className={styles.preview} aria-label={label} aria-describedby={id}
      onFocus={show} onBlur={hide} onClick={() => { pinned.current = true; show() }}>{text}</button>
    <div ref={preview} id={id} popover="auto" role="tooltip" className={styles.fullText}
      onToggle={event => { if (event.newState === 'closed') pinned.current = false }}>{text}</div>
  </span>
}

function MemoryDrawer({ owner, onClose }: { owner: string; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const close = useRef<HTMLButtonElement>(null)
  const title = useId()
  const client = useQueryClient()
  const key = ['memories', owner]
  const memories = useQuery({ queryKey: key, queryFn: ({ signal }) => api.memories(signal), refetchOnMount: 'always' })
  const [target, setTarget] = useState<MemoryItem | 'all' | null>(null)
  const deletion = useMutation({
    mutationFn: (item: MemoryItem | 'all') => item === 'all' ? api.deleteMemories() : api.deleteMemory(item.id),
    onSuccess: async () => {
      setTarget(null)
      await client.invalidateQueries({ queryKey: key })
    },
  })
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const element = dialog.current
    element?.showModal(); close.current?.focus()
    return () => { element?.close(); if (previous?.isConnected) previous.focus() }
  }, [])
  const count = memories.data?.categories.reduce((sum, category) => sum + category.items.length, 0) ?? 0
  function dismiss() { if (!deletion.isPending) onClose() }
  return <dialog ref={dialog} className={styles.drawer} aria-labelledby={title} onCancel={event => {
    event.preventDefault()
    if (deletion.isPending) return
    if (target) { setTarget(null); deletion.reset() } else onClose()
  }}>
    <header className={styles.drawerHeader}>
      <h2 id={title}>用户记忆</h2>
      <button ref={close} type="button" aria-label="关闭记忆列表" onClick={dismiss} disabled={deletion.isPending}><X size={20} /></button>
    </header>
    <p className={styles.note}>记忆在你的项目和会话间共享。关闭记忆后仍可查看和删除。</p>
    <div className={styles.actions}>
      <span>{count} 条记忆</span>
      <button type="button" onClick={() => void memories.refetch()} disabled={memories.isFetching || deletion.isPending || !!target}>
        <RefreshCw size={14} className={memories.isFetching ? 'spin' : undefined} />刷新</button>
      <button type="button" className={styles.danger} disabled={!count || deletion.isPending || !!target}
        onClick={() => { deletion.reset(); setTarget('all') }}><Trash2 size={14} />全部删除</button>
    </div>
    {memories.isPending && <p role="status">正在加载记忆…</p>}
    {memories.error && <div role="alert" className="error">{messageOf(memories.error)}
      <button type="button" onClick={() => void memories.refetch()} disabled={memories.isFetching}>重试加载</button>
    </div>}
    {memories.data && !count && <p className={styles.empty}>暂无记忆</p>}
    <div className={styles.groups} inert={!!target}>
      {memories.data?.categories.map(category => <section key={category.category} aria-label={category.label}>
        <h3>{category.label}<small>{category.category}</small><span>{category.items.length}</span></h3>
        {!category.items.length ? <p className={styles.emptyGroup}>暂无记忆</p> : <>
          <div className={`${styles.row} ${styles.columns}`} aria-hidden><span>ID</span><span>摘要</span><span>详情</span><span /></div>
          {category.items.map(item => <div className={styles.row} key={item.id}>
            <span className={styles.memoryId} title={String(item.id)}>{item.id}</span>
            <FullText text={item.abstract} label={`记忆 ${item.id} 摘要`} />
            <FullText text={item.detail} label={`记忆 ${item.id} 详情`} />
            <button type="button" className={styles.danger} aria-label={`删除记忆 ${item.id}`} disabled={deletion.isPending}
              onClick={() => { deletion.reset(); setTarget(item) }}><Trash2 size={15} /></button>
          </div>)}
        </>}
      </section>)}
    </div>
    {target && <DeleteConfirmation target={target} busy={deletion.isPending} error={deletion.error}
      onCancel={() => { setTarget(null); deletion.reset() }} onConfirm={() => { if (!deletion.isPending) deletion.mutate(target) }} />}
  </dialog>
}

function DeleteConfirmation({ target, busy, error, onCancel, onConfirm }: {
  target: MemoryItem | 'all'; busy: boolean; error: Error | null; onCancel: () => void; onConfirm: () => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const cancel = useRef<HTMLButtonElement>(null)
  const title = useId()
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const element = dialog.current
    element?.showModal(); cancel.current?.focus()
    return () => { element?.close(); if (previous?.isConnected) previous.focus() }
  }, [])
  return <dialog ref={dialog} className={styles.confirm} aria-labelledby={title} onCancel={event => {
    event.preventDefault(); event.stopPropagation(); if (!busy) onCancel()
  }}>
    <h2 id={title}>{target === 'all' ? '删除全部记忆？' : `删除记忆 ${target.id}？`}</h2>
    {target !== 'all' && <p className={styles.confirmAbstract}>{target.abstract}</p>}
    <p>删除后无法恢复，历史对话不会改变。运行中的任务仍可能生成新记忆。</p>
    {error && <div role="alert" className="error">{messageOf(error)}</div>}
    <div className={styles.confirmActions}>
      <button ref={cancel} type="button" onClick={onCancel} disabled={busy}>取消</button>
      <button type="button" className={styles.danger} onClick={onConfirm} disabled={busy}>{busy ? '正在删除…' : '确认删除'}</button>
    </div>
  </dialog>
}

export function MemoryControls({ owner }: { owner: string }) {
  const client = useQueryClient()
  const key = ['user-configs', owner]
  const configs = useQuery({ queryKey: key, queryFn: ({ signal }) => api.userConfigs(signal) })
  const [open, setOpen] = useState(false)
  const change = useMutation({
    mutationFn: (enabled: boolean) => api.updateUserConfig('enable_memory', enabled ? '1' : '0'),
    onMutate: async () => { await client.cancelQueries({ queryKey: key }) },
    onSuccess: data => { client.setQueryData(key, data) },
  })
  const enabled = configs.data?.configs.enable_memory === '1'
  return <>
    <div className={styles.controls}>
      <div className={styles.buttons}>
        <button type="button" role="switch" aria-label="记忆" aria-checked={enabled} className={styles.switch}
          disabled={!configs.data || change.isPending || configs.isFetching}
          onClick={() => change.mutate(!enabled)}><span className={styles.track}><span /></span>记忆</button>
        <span className={styles.hint}>下次任务生效</span>
        <button type="button" className={styles.open} onClick={() => setOpen(true)}>查看记忆</button>
      </div>
      {(configs.error || change.error) && <div role="alert" className="error">{messageOf(change.error ?? configs.error)}
        {configs.error && <button type="button" onClick={() => void configs.refetch()}>重试配置</button>}
      </div>}
    </div>
    {open && <MemoryDrawer owner={owner} onClose={() => setOpen(false)} />}
  </>
}
