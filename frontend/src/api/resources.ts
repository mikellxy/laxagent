import { json, request } from './http'
import { streamRun } from './sse'
import type { MessagePage, Models, Project, Run, Session, SessionDetail } from './types'

export const api = {
  projects: (signal?: AbortSignal) => json<Project[]>('/projects', { signal }),
  createProject: (name: string) => json<Project>('/projects', { method: 'POST', body: JSON.stringify({ name }) }),
  sessions: (project: number, signal?: AbortSignal) => json<Session[]>(`/projects/${project}/sessions`, { signal }),
  createSession: (project: number) => json<Session>(`/projects/${project}/sessions`, { method: 'POST' }),
  renameSession: (session: number, name: string) => json<Session>(`/sessions/${session}`, { method: 'PATCH', body: JSON.stringify({ name }) }),
  deleteSession: async (session: number): Promise<void> => { await request(`/sessions/${session}`, { method: 'DELETE' }) },
  cancelRun: (run: number, signal?: AbortSignal) => json<Run>(`/runs/${run}/cancel`, { method: 'POST', signal }),
  models: (signal?: AbortSignal) => json<Models>('/models', { signal }),
  detail: (session: number, signal?: AbortSignal) => json<SessionDetail>(`/sessions/${session}`, { signal }),
  messages: (session: number, before: number, signal?: AbortSignal) => json<MessagePage>(`/sessions/${session}/messages?before_seq=${before}&limit=50`, { signal }),
  chat: (session: number, query: string, model: string, signal?: AbortSignal) => json<{ run_id: number; status: 'waiting' }>(`/sessions/${session}/chat`, {
    method: 'POST', body: JSON.stringify({ user_query: query, model }), signal,
  }),
  stream: streamRun,
}
