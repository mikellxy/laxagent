export interface Project { id: number; user_id: string; name: string }
export interface Session extends Project { project_id: number; next_msg_seq: number }
export interface Run {
  run_id: number; session_id: number; status: 'waiting' | 'running' | 'paused' | 'success' | 'failed' | 'cancelled'
  model: string; error: string | null
  resume_time: number; error_code: string | null; error_desc: string | null
}
export interface SubmittedRun { run_id: number; session_id: number; status: 'waiting'; resume_time: number }
export interface SessionDetail extends Session { latest_run: Run | null; has_appendix?: boolean }
export interface AppendixReference { id: number; name: string }
export interface AppendixDisplay extends AppendixReference { kind: 'image' | 'file'; thumbnail_data_url: string | null }
export interface ReasoningPart { channel: 'text' | 'summary'; text: string }
export interface TextMessage { seq: number; role: 'user' | 'assistant'; content: string; reasoning?: ReasoningPart[]; appendix?: AppendixDisplay[]; complete?: boolean }
export interface WorkspaceFilePreview {
  path: string; name: string; size: number; kind: 'text' | 'image' | 'download'
  language: string | null; text: string | null; truncated: boolean; can_expand: boolean
}
export type ToolStatus = 'generating' | 'running' | 'completed' | 'failed' | 'interrupted' | 'unknown'
export interface ToolMessage {
  seq: number; role: 'tool'; tool_name: string; arguments: string
  answers?: Answer[]
  call_key?: string; status?: ToolStatus
  parent_seq?: number; call_order?: number
}
export type Message = TextMessage | ToolMessage
export interface MessagePage { items: Message[]; has_more: boolean; next_before_seq: number | null }
export interface Models { default_model: string; models: { id: string; provider: string; name: string; supports_multimodal?: boolean }[] }
export interface StreamEvent {
  run_id: number; msg_seq: number | null; type: string; role: string; error: string | null
  content: string | Record<string, unknown>
  channel?: 'text' | 'summary'
  resume_time?: number
  event_id?: string
}
export interface LoginResponse { access_token: string; token_type: string; expires_in: number; initial_session_id: number | null }

export interface UserConfigs { configs: Record<string, string> }
export interface MemoryItem { id: number; abstract: string; detail: string }
export interface Memories { categories: { category: string; label: string; items: MemoryItem[] }[] }

export interface Question { question: string; choice: string[] }
export interface Answer { question: string; answer: string }
export interface PendingQuestion {
  run_id: number; resume_time: number; interrupt_id: string; tool_call_id: string; questions: Question[]
}
