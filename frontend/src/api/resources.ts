import { json, request } from './http'
import { streamRun } from './sse'
import type { Answer, AppendixReference, UserConfigs, Memories, MessagePage, Models, Project, Run, Session, SessionDetail, SubmittedRun, WorkspaceFilePreview } from './types'

export const api = {
  workspacePreview: (session: number, path: string, signal?: AbortSignal) => json<WorkspaceFilePreview>(
    `/sessions/${session}/workspace-files/preview?${new URLSearchParams({ path })}`, { signal }),
  workspaceContent: async (session: number, path: string, download: boolean, signal?: AbortSignal) => (await request(
    `/sessions/${session}/workspace-files/content?${new URLSearchParams({ path, download: String(download) })}`, { signal })).blob(),
  uploadAppendix: (file: File, signal?: AbortSignal) => {
    const body = new FormData(); body.append('file', file)
    return json<AppendixReference>('/appendices', { method: 'POST', body, signal })
  },
  appendix: async (id: number, signal?: AbortSignal) => (await request(`/appendices/${id}`, { signal })).blob(),
  userConfigs: (signal?: AbortSignal) => json<UserConfigs>('/user-configs', { signal }),
  updateUserConfig: (key: string, value: string) => json<UserConfigs>('/user-configs', { method: 'PATCH', body: JSON.stringify({ key, value }) }),
  memories: (signal?: AbortSignal) => json<Memories>('/memories', { signal }),
  deleteMemory: async (id: number): Promise<void> => { await request(`/memories/${id}`, { method: 'DELETE' }) },
  deleteMemories: async (): Promise<void> => { await request('/memories', { method: 'DELETE' }) },
  projects: (signal?: AbortSignal) => json<Project[]>('/projects', { signal }),
  createProject: (name: string) => json<Project>('/projects', { method: 'POST', body: JSON.stringify({ name }) }),
  sessions: (project: number, signal?: AbortSignal) => json<Session[]>(`/projects/${project}/sessions`, { signal }),
  createSession: (project: number) => json<Session>(`/projects/${project}/sessions`, { method: 'POST' }),
  renameSession: (session: number, name: string) => json<Session>(`/sessions/${session}`, { method: 'PATCH', body: JSON.stringify({ name }) }),
  deleteSession: async (session: number): Promise<void> => { await request(`/sessions/${session}`, { method: 'DELETE' }) },
  cancelRun: (run: number, signal?: AbortSignal) => json<Run>(`/runs/${run}/cancel`, { method: 'POST', signal }),
  getRun: (run: number, signal?: AbortSignal) => json<Run>(`/runs/${run}`, { signal }),
  answerRun: (run: number, resumeTime: number, interruptId: string, answers: Answer[], signal?: AbortSignal) => json<SubmittedRun>(`/runs/${run}/answers`, {
    method: 'POST', body: JSON.stringify({ resume_time: resumeTime, interrupt_id: interruptId, answers }), signal,
  }),
  resumeRun: (run: number, resumeTime: number, signal?: AbortSignal) => json<SubmittedRun>(`/runs/${run}/resume`, {
    method: 'POST', body: JSON.stringify({ resume_time: resumeTime }), signal,
  }),
  models: (signal?: AbortSignal) => json<Models>('/models', { signal }),
  detail: (session: number, signal?: AbortSignal) => json<SessionDetail>(`/sessions/${session}`, { signal }),
  messages: (session: number, before: number, signal?: AbortSignal) => json<MessagePage>(`/sessions/${session}/messages?before_seq=${before}&limit=50`, { signal }),
  chat: (session: number, query: string, model: string, signal?: AbortSignal, appendix: AppendixReference[] = []) => json<SubmittedRun>(`/sessions/${session}/chat`, {
    method: 'POST', body: JSON.stringify({ user_query: query, model, appendix: appendix.map(({ id, name }) => ({ id, name })) }), signal,
  }),
  stream: streamRun,
}
