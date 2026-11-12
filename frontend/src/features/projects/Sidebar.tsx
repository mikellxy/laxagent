import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, Folder, Plus } from 'lucide-react'
import { useCallback, useState } from 'react'
import { api } from '../../api/resources'
import { NameDialog } from '../../components/NameDialog'
import { messageOf } from '../../api/errors'
import { SessionList } from '../sessions/SessionList'
import type { Project, Session } from '../../api/types'
import styles from './Sidebar.module.css'

async function laxcodeSession(client: QueryClient, owner: string, deleted: number): Promise<number | null> {
  try {
    const projects = await client.ensureQueryData({ queryKey: ['projects', owner], queryFn: ({ signal }) => api.projects(signal) })
    const project = projects.find(p => p.name === 'LaxCode')
    if (!project) return null
    const sessions = await client.ensureQueryData<Session[]>({ queryKey: ['sessions', owner, project.id], queryFn: ({ signal }) => api.sessions(project.id, signal) })
    const remaining = sessions.filter(s => s.id !== deleted)
    return (remaining.find(s => s.name === 'LaxCode') ?? remaining.at(-1))?.id ?? null
  } catch { return null }
}

function ProjectGroup({ project, owner, selected, onSelect, onDeleted }: { project: Project; owner: string; selected: number | null; onSelect: (id: number) => void; onDeleted: (id: number, next: number | null) => void }) {
  const client = useQueryClient()
  const [expanded, setExpanded] = useState(true)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  async function create() {
    if (creating) return
    setExpanded(true); setCreating(true); setError(null)
    try {
      const session = await api.createSession(project.id)
      await client.cancelQueries({ queryKey: ['sessions', owner, project.id] })
      client.setQueryData<Session[]>(['sessions', owner, project.id], old => [...(old ?? []), session].sort((a, b) => a.id - b.id))
      onSelect(session.id)
      void client.invalidateQueries({ queryKey: ['sessions', owner, project.id] })
    } catch (error) { setError(messageOf(error)) }
    finally { setCreating(false) }
  }
  return <section className={styles.project} aria-label={project.name}>
    <div className={styles.projectRow}>
      <Folder size={16} strokeWidth={1.6} /><span className={styles.projectName}>{project.name}</span>
      <button className={styles.newSession} aria-label={`${project.name} 新会话`} onClick={() => void create()} disabled={creating}><Plus size={13} />新会话</button>
      <button className={styles.toggle} aria-label={expanded ? `收起 ${project.name} 的会话列表` : `展开 ${project.name} 的会话列表`} aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
        {expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
      </button>
    </div>
    {error && <div role="alert" className="error">{error}</div>}
    {expanded && <SessionList projectId={project.id} owner={owner} selected={selected} onSelect={onSelect} onDeleted={onDeleted} creating={creating} onCreate={() => void create()} />}
  </section>
}
export function Sidebar({ owner, selected, onSelect, onDeleted }: { owner: string; selected: number | null; onSelect: (id: number) => void; onDeleted: (id: number, next: number | null) => void }) {
  const client = useQueryClient()
  const query = useQuery({ queryKey: ['projects', owner], queryFn: ({ signal }) => api.projects(signal) })
  const [expanded, setExpanded] = useState(true)
  const [creating, setCreating] = useState(false)
  // Deleting the selected session falls back to the LaxCode session when its project becomes empty.
  const deleted = useCallback((id: number, next: number | null) => {
    if (selected !== id || next !== null) { onDeleted(id, next); return }
    void laxcodeSession(client, owner, id).then(fallback => onDeleted(id, fallback))
  }, [client, owner, selected, onDeleted])
  return <aside className={styles.sidebar}>
    <div className={styles.heading}><span>项目</span>
      <button className={styles.newProject} onClick={() => setCreating(true)}><Plus size={14} />新建</button>
      <button className={styles.toggle} aria-label={expanded ? '收起项目列表' : '展开项目列表'} aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
        {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
      </button>
    </div>
    {query.error && <div role="alert" className="error">{messageOf(query.error)}</div>}
    {expanded && <nav className={styles.projects} aria-label="项目和会话">{query.data?.map(project =>
      <ProjectGroup key={project.id} project={project} owner={owner} selected={selected} onSelect={onSelect} onDeleted={deleted} />)}</nav>}
    {creating && <NameDialog title="新建项目" onClose={() => setCreating(false)} onSave={async name => {
      const project = await api.createProject(name)
      client.setQueryData<Project[]>(['projects', owner], old => [...(old ?? []), project].sort((a, b) => a.id - b.id))
      setExpanded(true)
    }} />}
  </aside>
}
