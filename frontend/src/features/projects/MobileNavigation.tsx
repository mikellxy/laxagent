import { useEffect, useRef, useState, type ComponentProps } from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import { Check, Copy, Menu, X } from 'lucide-react'
import { api } from '../../api/resources'
import { MemoryControls } from '../memory/MemoryControls'
import { Sidebar } from './Sidebar'

export function MobileNavigation(props: ComponentProps<typeof Sidebar>) {
  const [open, setOpen] = useState(false)
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const dialog = useRef<HTMLDialogElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const projects = useQuery({ queryKey: ['projects', props.owner], queryFn: ({ signal }) => api.projects(signal) })
  const sessions = useQueries({ queries: (projects.data ?? []).map(project => ({
    queryKey: ['sessions', props.owner, project.id], queryFn: ({ signal }: { signal: AbortSignal }) => api.sessions(project.id, signal),
  })) })
  const current = sessions.flatMap(query => query.data ?? []).find(session => session.id === props.selected)
  useEffect(() => {
    if (open) dialog.current?.showModal()
    else if (dialog.current?.open) { dialog.current.close(); trigger.current?.focus() }
  }, [open])
  useEffect(() => {
    if (copyState === 'idle') return
    const timer = setTimeout(() => setCopyState('idle'), 2000)
    return () => clearTimeout(timer)
  }, [copyState])
  async function copySession() {
    try {
      await navigator.clipboard.writeText(`user_id: ${props.owner}\nsession_id: ${props.selected}`)
      setCopyState('copied')
    } catch { setCopyState('failed') }
  }
  return <>
    <header className="mobileHeader">
      <button ref={trigger} aria-label="打开项目和设置" aria-expanded={open} aria-controls="mobile-navigation" onClick={() => setOpen(true)}><Menu size={21} /></button>
      <span>{current?.name ?? '选择会话'}</span>
      {props.selected !== null && <button className="copySession" aria-label="复制会话 ID" onClick={() => void copySession()}>
        {copyState === 'copied' ? <Check size={16} /> : <Copy size={16} />}<span aria-live="polite">{copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败' : '复制会话 ID'}</span>
      </button>}
    </header>
    <dialog ref={dialog} id="mobile-navigation" className="mobileNavigation" aria-label="项目和设置"
      onCancel={event => { event.preventDefault(); setOpen(false) }}
      onClick={event => { if (event.target === event.currentTarget) setOpen(false) }}>
      <div className="mobileNavigationContent">
        <header className="mobileNavigationHeading"><strong>LaxCode</strong><button aria-label="关闭项目和设置" onClick={() => setOpen(false)}><X size={21} /></button></header>
        <Sidebar {...props} onSelect={id => { props.onSelect(id); setOpen(false) }} />
        <div className="mobileSettings">
          <MemoryControls owner={props.owner} />
        </div>
      </div>
    </dialog>
  </>
}
