import { useEffect, useId, useRef, useState } from 'react'
import { messageOf } from '../api/errors'
import styles from './NameDialog.module.css'

export function DeleteSessionDialog({ name, onDelete, onClose }: {
  name: string; onDelete: () => Promise<void>; onClose: () => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const cancel = useRef<HTMLButtonElement>(null)
  const title = useId()
  const [busy, setBusy] = useState(false)
  const submitting = useRef(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const element = dialog.current
    element?.showModal(); cancel.current?.focus()
    return () => { element?.close(); if (previous?.isConnected) previous.focus() }
  }, [])
  return <dialog ref={dialog} className={styles.dialog} aria-labelledby={title} onCancel={event => {
    event.preventDefault(); if (!submitting.current) onClose()
  }}>
    <form onSubmit={async event => {
      event.preventDefault(); if (submitting.current) return
      submitting.current = true; setBusy(true); setError(null)
      try { await onDelete(); onClose() }
      catch (error) { setError(messageOf(error)); submitting.current = false; setBusy(false) }
    }}>
      <h2 id={title}>删除会话？</h2>
      <p className={styles.description}>“{name}”将从会话列表中移除，正在执行的任务也会停止。</p>
      {error && <div role="alert" className="error">{error}</div>}
      <div className={styles.actions}>
        <button ref={cancel} type="button" onClick={onClose} disabled={busy}>取消</button>
        <button className={styles.danger} type="submit" disabled={busy}>{busy ? '正在删除…' : '删除'}</button>
      </div>
    </form>
  </dialog>
}
