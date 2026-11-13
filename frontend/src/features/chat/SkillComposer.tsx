import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Box } from 'lucide-react'
import { api } from '../../api/resources'
import type { Skill } from '../../api/types'
import { copySkillText, editorText, matchesSkill, skillMarker, skillParts, slashQuery } from './skill-text'
import styles from './Skills.module.css'

type Selection = { start: number; end: number }
type Entry = Selection & { text: string }

function selectionIn(root: HTMLElement): Selection | null {
  const selection = window.getSelection()
  if (!selection?.rangeCount) return null
  const range = selection.getRangeAt(0)
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return null
  const prefix = range.cloneRange()
  prefix.selectNodeContents(root); prefix.setEnd(range.startContainer, range.startOffset)
  const start = editorText(prefix.cloneContents()).length
  prefix.setEnd(range.endContainer, range.endOffset)
  return { start, end: editorText(prefix.cloneContents()).length }
}

function restoreSelection(root: HTMLElement, selection: Selection) {
  function point(offset: number): [Node, number] {
    let remaining = offset
    for (let i = 0; i < root.childNodes.length; i++) {
      const node = root.childNodes[i]
      const length = editorText(node).length
      if (remaining <= length) {
        if (node.nodeType === Node.TEXT_NODE) return [node, remaining]
        return [root, i + (remaining > 0 ? 1 : 0)]
      }
      remaining -= length
    }
    return [root, root.childNodes.length]
  }
  const range = document.createRange()
  range.setStart(...point(selection.start)); range.setEnd(...point(selection.end))
  const current = window.getSelection()
  current?.removeAllRanges(); current?.addRange(range)
}

function renderEditor(root: HTMLElement, text: string) {
  const fragment = document.createDocumentFragment()
  for (const part of skillParts(text)) {
    if (!part.name) fragment.append(document.createTextNode(part.text))
    else {
      const badge = document.createElement('span')
      badge.className = styles.skill; badge.contentEditable = 'false'
      badge.dataset.skillRaw = part.text; badge.textContent = part.name
      badge.setAttribute('aria-label', `技能 ${part.name}`)
      fragment.append(badge)
    }
  }
  if (text.endsWith('\n')) {
    const tail = document.createElement('br'); tail.dataset.tail = ''; fragment.append(tail)
  }
  root.replaceChildren(fragment)
}

export function SkillComposer({ value, onChange, onSend, mobile }: {
  value: string; onChange: (value: string) => void; onSend: () => void; mobile: boolean
}) {
  const root = useRef<HTMLDivElement>(null)
  const current = useRef<Entry>({ text: value, start: value.length, end: value.length })
  const undo = useRef<Entry[]>([]), redo = useRef<Entry[]>([])
  const composing = useRef(false)
  const [trigger, setTrigger] = useState<ReturnType<typeof slashQuery>>(null)
  const dismissed = useRef<number | null>(null)
  const [catalog, setCatalog] = useState<Skill[]>([])
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [active, setActive] = useState(0)
  const [position, setPosition] = useState({ left: 0, bottom: 0, width: 0, maxHeight: 240 })
  const menu = useRef<HTMLDivElement>(null)
  const id = useId()
  const open = trigger !== null
  const results = status === 'ready' ? catalog.filter(skill => matchesSkill(skill, trigger?.query ?? '')) : []

  function inspect() {
    if (!root.current || composing.current || document.activeElement !== root.current) return
    const selection = selectionIn(root.current)
    if (!selection) { setTrigger(null); return }
    current.current = { ...current.current, ...selection }
    const next = slashQuery(current.current.text, selection.start, selection.end)
    if (!next || next.start !== dismissed.current) dismissed.current = null
    setTrigger(next && next.start !== dismissed.current ? next : null)
  }

  function commit(text: string, selection: Selection, record = true) {
    if (!root.current) return
    if (record && text !== current.current.text) { undo.current.push({ ...current.current }); if (undo.current.length > 100) undo.current.shift(); redo.current = [] }
    current.current = { text, ...selection }
    renderEditor(root.current, text)
    restoreSelection(root.current, selection)
    onChange(text); inspect()
  }

  function replace(text: string, selection = selectionIn(root.current!) ?? current.current) {
    const old = current.current.text
    const next = old.slice(0, selection.start) + text + old.slice(selection.end)
    const caret = selection.start + text.length
    commit(next, { start: caret, end: caret })
  }

  function history(back: boolean) {
    const from = back ? undo.current : redo.current, to = back ? redo.current : undo.current
    const next = from.pop()
    if (!next) return
    to.push({ ...current.current }); commit(next.text, next, false)
  }

  function deleteToken(backward: boolean) {
    const selection = selectionIn(root.current!)
    if (!selection || selection.start !== selection.end) return false
    let offset = 0
    for (const part of skillParts(current.current.text)) {
      const end = offset + part.text.length
      if (part.name && (backward ? selection.start === end : selection.start === offset)) {
        replace('', { start: offset, end }); return true
      }
      offset = end
    }
    return false
  }

  function choose(skill: Skill) {
    if (!trigger) return
    root.current?.focus()
    replace(skillMarker(skill.name), { start: trigger.start, end: trigger.end })
    setTrigger(null)
  }

  useLayoutEffect(() => {
    if (!root.current || value === current.current.text && editorText(root.current) === value) return
    const focused = document.activeElement === root.current
    current.current = { text: value, start: value.length, end: value.length }
    renderEditor(root.current, value)
    undo.current = []; redo.current = []
    if (focused) restoreSelection(root.current, current.current)
    setTrigger(null)
  }, [value])

  useEffect(() => {
    document.addEventListener('selectionchange', inspect)
    return () => document.removeEventListener('selectionchange', inspect)
  })

  // Use the native event: React's beforeinput polyfill omits mobile/history input types.
  useEffect(() => {
    const element = root.current!
    const beforeInput = (event: InputEvent) => {
      if (composing.current || event.isComposing) return
      const selection = selectionIn(element)
      if (selection) current.current = { ...current.current, ...selection }
      if (event.inputType === 'historyUndo' || event.inputType === 'historyRedo') {
        event.preventDefault(); history(event.inputType === 'historyUndo')
      } else if (event.inputType === 'insertParagraph' || event.inputType === 'insertLineBreak') {
        event.preventDefault()
        if (open) { if (results.length) choose(results[active] ?? results[0]) }
        else replace('\n')
      } else if (['deleteContentBackward', 'deleteContentForward'].includes(event.inputType)
        && deleteToken(event.inputType === 'deleteContentBackward')) event.preventDefault()
    }
    element.addEventListener('beforeinput', beforeInput)
    return () => element.removeEventListener('beforeinput', beforeInput)
  })

  const slashStart = trigger?.start
  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    setStatus('loading'); setCatalog([]); setActive(0)
    void api.skills(controller.signal).then(data => {
      if (!controller.signal.aborted) { setCatalog(data.skills); setStatus('ready') }
    }, () => { if (!controller.signal.aborted) setStatus('error') })
    return () => controller.abort()
  }, [open, slashStart])
  useEffect(() => { setActive(0) }, [trigger?.query])
  useLayoutEffect(() => { menu.current?.querySelector('[aria-selected="true"]')?.scrollIntoView?.({ block: 'nearest' }) }, [active])

  useLayoutEffect(() => {
    if (!open || !root.current) return
    const anchor = root.current.closest('[data-composer]') ?? root.current
    const update = () => {
      const rect = anchor.getBoundingClientRect()
      const viewport = window.visualViewport
      const top = viewport?.offsetTop ?? 0
      setPosition({ left: rect.left, width: rect.width, bottom: window.innerHeight - rect.top + 8,
        maxHeight: Math.max(44, Math.min(240, rect.top - top - 16)) })
    }
    update()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update)
    observer?.observe(anchor)
    window.addEventListener('resize', update); window.addEventListener('scroll', update, true)
    window.visualViewport?.addEventListener('resize', update); window.visualViewport?.addEventListener('scroll', update)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', update); window.removeEventListener('scroll', update, true)
      window.visualViewport?.removeEventListener('resize', update); window.visualViewport?.removeEventListener('scroll', update)
    }
  }, [open])

  return <>
    <div ref={root} className={styles.editor} role="textbox" aria-label="消息输入" aria-multiline="true"
      aria-autocomplete="list" aria-expanded={open} aria-controls={open ? id : undefined}
      aria-activedescendant={open && results[active] ? `${id}-${active}` : undefined}
      data-placeholder="向 LaxAgent 提问" contentEditable suppressContentEditableWarning
      onFocus={inspect} onBlur={() => setTrigger(null)}
      onCompositionStart={() => { composing.current = true; setTrigger(null) }}
      onCompositionEnd={() => {
        composing.current = false
        const selection = selectionIn(root.current!) ?? current.current
        commit(editorText(root.current!), selection)
      }}
      onInput={() => {
        if (composing.current) return
        const selection = selectionIn(root.current!) ?? current.current
        commit(editorText(root.current!), selection)
      }}
      onCopy={copySkillText}
      onCut={event => {
        const selection = selectionIn(root.current!)
        if (!selection || selection.start === selection.end) return
        event.clipboardData.setData('text/plain', current.current.text.slice(selection.start, selection.end))
        event.preventDefault(); replace('', selection)
      }}
      onPaste={event => { event.preventDefault(); replace(event.clipboardData.getData('text/plain').replace(/\r\n?/g, '\n')) }}
      onDrop={event => { event.preventDefault() }}
      onKeyDown={event => {
        if (composing.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
        const modifier = event.ctrlKey || event.metaKey
        if (modifier && (event.key.toLowerCase() === 'z' || event.key.toLowerCase() === 'y')) {
          event.preventDefault(); history(event.key.toLowerCase() === 'z' && !event.shiftKey); return
        }
        if (open && event.key === 'Escape') { event.preventDefault(); dismissed.current = trigger.start; setTrigger(null); return }
        if (open && ['ArrowDown', 'ArrowUp', 'Enter'].includes(event.key)) {
          event.preventDefault()
          if (!results.length) return
          if (event.key === 'Enter') choose(results[active] ?? results[0])
          else setActive(index => (index + (event.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length)
          return
        }
        if (event.key === 'Enter') {
          event.preventDefault()
          if (!mobile && !event.shiftKey) onSend()
          else replace('\n')
          return
        }
        if (!modifier && ['Backspace', 'Delete'].includes(event.key) && deleteToken(event.key === 'Backspace')) event.preventDefault()
      }} />
    {open && createPortal(<div ref={menu} id={id} role="listbox" aria-label="选择技能" className={styles.menu} style={position}>
      {status !== 'ready' || !results.length
        ? <div className={styles.status} role="status">{status === 'loading' ? '加载中…' : status === 'error' ? '加载失败，请关闭后重试' : '无匹配技能'}</div>
        : results.map((skill, index) => <button type="button" role="option" id={`${id}-${index}`} key={skill.name}
          aria-selected={index === active} aria-label={`${skill.name} ${skill.desc}`} className={styles.option} tabIndex={-1} title={`${skill.name} — ${skill.desc}`}
          onPointerDown={event => event.preventDefault()} onMouseDown={event => event.preventDefault()} onMouseEnter={() => setActive(index)} onClick={() => choose(skill)}>
          <Box size={19} /><span className={styles.name}>{skill.name}</span><span className={styles.desc}>{skill.desc}</span>
        </button>)}
    </div>, document.body)}
  </>
}
