import { useCallback, useEffect, useState } from 'react'
import { LoaderCircle } from 'lucide-react'
import { bootstrap, guestLogin } from '../features/auth/session'
import { Sidebar } from '../features/projects/Sidebar'
import { MemoryControls } from '../features/memory/MemoryControls'
import { Chat, UserIdBar } from '../features/chat/Chat'
import { selectedSession, selectSession } from '../lib/preferences'
import { messageOf } from '../api/errors'

export function App() {
  const [owner, setOwner] = useState<string | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [retryBootstrap, setRetryBootstrap] = useState(false)
  const load = useCallback(async () => {
    setBusy(true); setError(null)
    try {
      const id = await bootstrap(); setOwner(id)
      if (id) setSelected(selectedSession(id))
      setRetryBootstrap(false)
    } catch (error) { setError(messageOf(error)); setRetryBootstrap(true) }
    finally { setBusy(false) }
  }, [])
  useEffect(() => { void load() }, [load])
  const select = useCallback((id: number | null) => { setSelected(id); if (owner) selectSession(owner, id) }, [owner])
  const missing = useCallback(() => select(null), [select])
  const deleted = useCallback((id: number) => setSelected(current => current === id ? null : current), [])
  if (!owner) return <div className="login">
    {error && <div role="alert" className="error">{error}</div>}
    {busy ? <LoaderCircle size={20} className="spin" aria-label="正在登录" /> :
      <button className="guest" onClick={async () => {
        if (retryBootstrap) { void load(); return }
        setBusy(true); setError(null)
        try {
          const result = await guestLogin()
          selectSession(result.owner, result.session)
          setSelected(result.session); setOwner(result.owner)
        } catch (error) { setError(messageOf(error)) }
        finally { setBusy(false) }
      }}>{retryBootstrap ? '重试' : '以游客登录'}</button>}
    <span aria-hidden className="loginWatermark">LAXCODE</span>
  </div>
  return <div className="shell"><Sidebar owner={owner} selected={selected} onSelect={select} onDeleted={deleted} />
    <div className="mainColumn">
    <header className="mainHeader">
      {selected !== null && <UserIdBar key={`${owner}:${selected}`} owner={owner} sessionId={selected} />}
      <MemoryControls key={owner} owner={owner} />
    </header>
    {selected !== null ? <Chat key={`${owner}:${selected}`} owner={owner} sessionId={selected} onMissing={missing} /> : <main className="blank" />}</div>
  </div>
}
