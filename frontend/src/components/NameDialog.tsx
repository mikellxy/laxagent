import { useEffect, useRef, useState } from 'react'
import { messageOf } from '../api/errors'
import styles from './NameDialog.module.css'

export function NameDialog({ title, initial = '', onSave, onClose }: {
  title: string; initial?: string; onSave: (name: string) => Promise<void>; onClose: () => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [name, setName] = useState(initial)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    dialog.current?.showModal()
    dialog.current?.querySelector('input')?.select()
    return () => { dialog.current?.close(); previous?.focus() }
  }, [])
  return <dialog ref={dialog} className={styles.dialog} aria-labelledby="dialog-title" onCancel={event => {
    event.preventDefault(); if (!busy) onClose()
  }}>
    <form onSubmit={async event => {
      event.preventDefault(); if (busy || !name.trim()) return
      setBusy(true); setError(null)
      try { await onSave(name.trim()); onClose() } catch (error) { setError(messageOf(error)); setBusy(false) }
    }}>
      <h2 id="dialog-title">{title}</h2>
      <input autoFocus aria-label="名称" value={name} onChange={e => setName(e.target.value)} disabled={busy} />
      {error && <div role="alert" className="error">{error}</div>}
      <div className={styles.actions}>
        <button type="button" onClick={onClose} disabled={busy}>取消</button>
        <button className={styles.primary} type="submit" disabled={busy || !name.trim()}>确定</button>
      </div>
    </form>
  </dialog>
}
