import { useQuery, useQueryClient } from '@tanstack/react-query'
import { MessageSquare, Pencil, Plus } from 'lucide-react'
import { useState } from 'react'
import { api } from '../../api/resources'
import { NameDialog } from '../../components/NameDialog'
import { messageOf } from '../../api/errors'
import type { Session } from '../../api/types'
import styles from '../projects/Sidebar.module.css'

export function SessionList({ projectId, owner, selected, onSelect, creating, onCreate }: {
  projectId: number; owner: string; selected: number | null; onSelect: (id: number) => void; creating: boolean; onCreate: () => void
}) {
  const client = useQueryClient()
  const query = useQuery({ queryKey: ['sessions', owner, projectId], queryFn: ({ signal }) => api.sessions(projectId, signal) })
  const [renaming, setRenaming] = useState<Session | null>(null)
  return <>
    {query.error && <div className="error" role="alert">{messageOf(query.error)}</div>}
    <div className={styles.sessions}>
      {query.data?.map(session => <div key={session.id} className={`${styles.sessionRow} ${selected === session.id ? styles.selected : ''}`}>
        <button className={styles.sessionName} onClick={() => onSelect(session.id)} aria-current={selected === session.id ? 'page' : undefined}>
          <MessageSquare size={15} strokeWidth={1.6} /><span>{session.name}</span>
        </button>
        <button className={styles.rename} aria-label={`修改会话名 ${session.name}`} onClick={() => setRenaming(session)}><Pencil size={13} /></button>
      </div>)}
      {query.data?.length === 0 && <button className={styles.emptyCreate} onClick={onCreate} disabled={creating}><Plus size={14} />新建会话</button>}
    </div>
    {renaming && <NameDialog title="修改会话名" initial={renaming.name} onClose={() => setRenaming(null)} onSave={async name => {
      const updated = await api.renameSession(renaming.id, name)
      client.setQueryData<Session[]>(['sessions', owner, projectId], old => old?.map(s => s.id === updated.id ? updated : s))
    }} />}
  </>
}
