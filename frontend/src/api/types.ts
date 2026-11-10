export interface Project { id: number; user_id: string; name: string }
export interface Session extends Project { project_id: number; next_msg_seq: number }
export interface Run {
  run_id: number; session_id: number; status: 'waiting' | 'running' | 'success' | 'failed' | 'cancelled'
  model: string; error: string | null
}
export interface SessionDetail extends Session { latest_run: Run | null }
export interface ReasoningPart { channel: 'text' | 'summary'; text: string }
export interface TextMessage { seq: number; role: 'user' | 'assistant'; content: string; reasoning?: ReasoningPart[] }
export interface ToolMessage { seq: number; role: 'tool'; tool_name: string; arguments: string }
export type Message = TextMessage | ToolMessage
export interface MessagePage { items: Message[]; has_more: boolean; next_before_seq: number | null }
export interface Models { default_model: string; models: { id: string; provider: string; name: string }[] }
export interface StreamEvent {
  run_id: number; msg_seq: number | null; type: string; role: string; error: string | null
  content: string | Record<string, unknown>
  channel?: 'text' | 'summary'
}
export interface LoginResponse { access_token: string; token_type: string; expires_in: number; initial_session_id: number | null }
