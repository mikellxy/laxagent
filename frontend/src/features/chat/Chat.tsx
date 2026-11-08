import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, ChevronDown, LoaderCircle, Wrench } from 'lucide-react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../../api/resources'
import { chooseModel, sessionModel } from '../../lib/preferences'
import { messageOf } from '../../api/errors'
import { Conversation } from './conversation'
import type { Message } from '../../api/types'
import styles from './Chat.module.css'

function argumentsText(value: string) { try { return JSON.stringify(JSON.parse(value), null, 2) } catch { return value } }
function MessageCard({ message }: { message: Message }) {
  if (message.role === 'assistant' && !message.content) return null
  return <article data-seq={message.seq} aria-label={message.role === 'user' ? '用户消息' : message.role === 'assistant' ? '助手消息' : '工具调用'}
    className={`${styles.message} ${styles[message.role]}`}>
    {message.role === 'tool' ? <><div className={styles.toolTitle}><Wrench size={14} />{message.tool_name}</div><pre>{argumentsText(message.arguments)}</pre></>
      : message.role === 'assistant' ? <Markdown remarkPlugins={[remarkGfm]} skipHtml components={{
        a: ({ children, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer">{children}</a>,
      }}>{message.content}</Markdown> : message.content}
  </article>
}

export function Chat({ owner, sessionId, onMissing }: { owner: string; sessionId: number; onMissing: () => void }) {
  const conversation = useMemo(() => new Conversation(sessionId), [sessionId])
  const snapshot = useSyncExternalStore(conversation.subscribe, conversation.getSnapshot)
  const catalog = useQuery({ queryKey: ['models', owner], queryFn: ({ signal }) => api.models(signal) })
  const [model, setModel] = useState('')
  const [draft, setDraft] = useState('')
  const [following, setFollowing] = useState(true)
  const follow = useRef(true)
  const scroller = useRef<HTMLDivElement>(null)
  const textarea = useRef<HTMLTextAreaElement>(null)
  const programmatic = useRef(false)
  const lastTop = useRef(0)
  const anchor = useRef<{ seq: string; offset: number } | null>(null)

  useEffect(() => {
    void conversation.refresh()
    const visible = () => { if (document.visibilityState === 'visible') void conversation.refresh() }
    document.addEventListener('visibilitychange', visible)
    return () => { document.removeEventListener('visibilitychange', visible); conversation.dispose() }
  }, [conversation])
  useEffect(() => { if (snapshot.missing) onMissing() }, [snapshot.missing, onMissing])
  useEffect(() => { if (catalog.data) setModel(sessionModel(owner, sessionId, catalog.data)) }, [catalog.data, owner, sessionId])
  const setFollow = useCallback((value: boolean) => { follow.current = value; setFollowing(value) }, [])
  function moveScroll(fn: (element: HTMLDivElement) => void) {
    if (!scroller.current) return
    programmatic.current = true; fn(scroller.current); lastTop.current = scroller.current.scrollTop
    requestAnimationFrame(() => { programmatic.current = false })
  }
  useLayoutEffect(() => {
    const element = scroller.current
    if (!element) return
    if (anchor.current) {
      const saved = anchor.current
      const row = element.querySelector<HTMLElement>(`[data-seq="${saved.seq}"]`)
      if (row) moveScroll(el => { el.scrollTop += row.getBoundingClientRect().top - el.getBoundingClientRect().top - saved.offset })
      if (!snapshot.loadingOlder) anchor.current = null
    } else if (follow.current) moveScroll(el => { el.scrollTop = el.scrollHeight })
  }, [snapshot.messages, snapshot.pending, snapshot.loadingOlder])
  useLayoutEffect(() => {
    if (textarea.current) { textarea.current.style.height = 'auto'; textarea.current.style.height = `${Math.min(textarea.current.scrollHeight, 200)}px` }
  }, [draft])
  async function older() {
    const element = scroller.current
    if (!element || snapshot.loadingOlder || snapshot.syncing || !snapshot.hasMore || follow.current) return
    const row = [...element.querySelectorAll<HTMLElement>('[data-seq]')].find(row => row.getBoundingClientRect().bottom > element.getBoundingClientRect().top)
    if (row) anchor.current = { seq: row.dataset.seq!, offset: row.getBoundingClientRect().top - element.getBoundingClientRect().top }
    await conversation.loadOlder()
  }
  async function send() {
    if (!draft.trim() || !model || snapshot.busy || !snapshot.ready) return
    const submitted = draft
    if (await conversation.send(submitted, model)) setDraft(current => current === submitted ? '' : current)
  }
  const rows: { key: string; seq: number; node: React.ReactNode }[] = snapshot.messages.map(message => ({ key: String(message.seq), seq: message.seq, node: <MessageCard message={message} /> }))
  if (snapshot.pending) rows.push({ key: 'pending', seq: snapshot.pending.afterSeq + 0.5,
    node: <article className={`${styles.message} ${styles.user} ${snapshot.pending.failed ? styles.failed : ''}`} aria-label="待确认用户消息">{snapshot.pending.content}</article> })
  rows.sort((a, b) => a.seq - b.seq)
  return <main className={styles.chat} aria-label="聊天">
    <div className={styles.scrollArea} ref={scroller} aria-label="消息列表" tabIndex={0}
      onWheel={event => { if (event.deltaY < 0 && scroller.current && scroller.current.scrollHeight > scroller.current.clientHeight) setFollow(false) }}
      onScroll={() => {
        const element = scroller.current!
        if (!programmatic.current) {
          if (element.scrollTop < lastTop.current - 1) setFollow(false)
          if (element.scrollHeight - element.clientHeight - element.scrollTop <= 2) setFollow(true)
        }
        lastTop.current = element.scrollTop
        if (element.scrollTop < 160) void older()
      }}>
      <div className={styles.timeline}>{rows.map(row => <div key={row.key}>{row.node}</div>)}</div>
    </div>
    <div className={styles.footer}>
      {!following && <button className={styles.latest} onClick={() => { anchor.current = null; setFollow(true); moveScroll(el => { el.scrollTop = el.scrollHeight }) }}><ArrowDown size={14} />回到最新</button>}
      {(snapshot.error || catalog.error) && <div className="error" role="alert">{snapshot.error ?? messageOf(catalog.error)}</div>}
      <div className={styles.composer}>
        <textarea ref={textarea} aria-label="消息输入" value={draft} rows={2} onChange={e => setDraft(e.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229) { event.preventDefault(); void send() }
          }} />
        <div className={styles.toolbar}>
          <div className={styles.model}>
            <select aria-label="模型" value={model} disabled={!catalog.data} onChange={event => {
              setModel(event.target.value); chooseModel(owner, sessionId, event.target.value)
            }}>{catalog.data?.models.map(item => <option key={item.id} value={item.id}>{item.id}</option>)}</select>
            <ChevronDown size={13} />
          </div>
          <button className={styles.send} aria-label="发送消息" onClick={() => void send()} disabled={!draft.trim() || !model || snapshot.busy || !snapshot.ready}>
            {snapshot.busy ? <LoaderCircle size={18} className="spin" /> : <ArrowUp size={19} strokeWidth={2} />}
          </button>
        </div>
      </div>
    </div>
  </main>
}
