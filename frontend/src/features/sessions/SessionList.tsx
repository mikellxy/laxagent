import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Ellipsis, MessageSquare, Pencil, Plus, Trash2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { api } from '../../api/resources'
import { NameDialog } from '../../components/NameDialog'
import { DeleteSessionDialog } from '../../components/DeleteSessionDialog'
import { forgetSession } from '../../lib/preferences'
import { messageOf } from '../../api/errors'
import type { Session } from '../../api/types'
import styles from '../projects/Sidebar.module.css'
import { useMobileLayout } from '../../lib/mobile'

function SessionActions({ session, close, rename, remove }: { session: Session; close: () => void; rename: () => void; remove: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const element = dialog.current
    element?.showModal()
    return () => { element?.close(); if (previous?.isConnected) previous.focus() }
  }, [])
  return <dialog ref={dialog} className={styles.actionsDialog} aria-label={`会话操作 ${session.name}`}
    onCancel={event => { event.preventDefault(); close() }}>
    <strong>{session.name}</strong>
    <button onClick={rename}><Pencil size={18} />修改会话名</button>
    <button className={styles.delete} onClick={remove}><Trash2 size={18} />删除会话</button>
    <button onClick={close}>取消</button>
  </dialog>
}

export function SessionList({ projectId, owner, selected, onSelect, onDeleted, creating, onCreate }: {
  projectId: number; owner: string; selected: number | null; onSelect: (id: number) => void
  onDeleted: (id: number, next: number | null) => void; creating: boolean; onCreate: () => void
}) {
  const client = useQueryClient()
  const query = useQuery({ queryKey: ['sessions', owner, projectId], queryFn: ({ signal }) => api.sessions(projectId, signal) })
  const [renaming, setRenaming] = useState<Session | null>(null)
  const [deleting, setDeleting] = useState<Session | null>(null)
  const [managing, setManaging] = useState<Session | null>(null)
  const mobile = useMobileLayout()
  return <>
    {query.error && <div className="error" role="alert">{messageOf(query.error)}</div>}
    <div className={styles.sessions}>
      {query.data?.map(session => <div key={session.id} className={`${styles.sessionRow} ${selected === session.id ? styles.selected : ''}`}>
        <button className={styles.sessionName} onClick={() => onSelect(session.id)} aria-current={selected === session.id ? 'page' : undefined}>
          <MessageSquare size={15} strokeWidth={1.6} /><span>{session.name}</span>
        </button>
        {mobile ? <button className={styles.more} aria-label={`更多会话操作 ${session.name}`} onClick={() => setManaging(session)}><Ellipsis size={20} /></button> : <>
          <button className={styles.rename} aria-label={`修改会话名 ${session.name}`} onClick={() => setRenaming(session)}><Pencil size={13} /></button>
          <button className={`${styles.rename} ${styles.delete}`} aria-label={`删除会话 ${session.name}`} onClick={() => setDeleting(session)}><Trash2 size={13} /></button>
        </>}
      </div>)}
      {query.data?.length === 0 && <button className={styles.emptyCreate} onClick={onCreate} disabled={creating}><Plus size={14} />新建会话</button>}
    </div>
    {managing && <SessionActions session={managing} close={() => setManaging(null)}
      rename={() => { setRenaming(managing); setManaging(null) }} remove={() => { setDeleting(managing); setManaging(null) }} />}
    {renaming && <NameDialog title="修改会话名" initial={renaming.name} onClose={() => setRenaming(null)} onSave={async name => {
      const updated = await api.renameSession(renaming.id, name)
      client.setQueryData<Session[]>(['sessions', owner, projectId], old => old?.map(s => s.id === updated.id ? updated : s))
    }} />}
    {deleting && <DeleteSessionDialog name={deleting.name} onClose={() => setDeleting(null)} onDelete={async () => {
      const sessions = query.data ?? []
      const index = sessions.findIndex(s => s.id === deleting.id)
      const next = index < 0 ? null : sessions[index - 1]?.id ?? sessions[index + 1]?.id ?? null
      await api.deleteSession(deleting.id)
      await client.cancelQueries({ queryKey: ['sessions', owner, projectId] })
      client.setQueryData<Session[]>(['sessions', owner, projectId], old => old?.filter(s => s.id !== deleting.id))
      forgetSession(owner, deleting.id)
      onDeleted(deleting.id, next)
      void client.invalidateQueries({ queryKey: ['sessions', owner, projectId] })
    }} />}
  </>
}
