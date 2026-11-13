import type { Skill } from '../../api/types'

export const skillMarker = (name: string) => ` 用户指定使用技能${name} `
export type SkillPart = { text: string; name?: string }

export function skillParts(text: string): SkillPart[] {
  const parts: SkillPart[] = []
  const pattern = / 用户指定使用技能([a-z0-9]+(?:-[a-z0-9]+)*) /g
  let start = 0
  for (const match of text.matchAll(pattern)) {
    if (match[1].length > 64) continue
    if (match.index! > start) parts.push({ text: text.slice(start, match.index) })
    parts.push({ text: match[0], name: match[1] })
    start = match.index! + match[0].length
  }
  if (start < text.length) parts.push({ text: text.slice(start) })
  return parts
}

export function matchesSkill(skill: Skill, query: string): boolean {
  const wanted = Array.from(query.toLowerCase())
  return [skill.name, skill.desc].some(text => {
    let index = 0
    for (const char of text.toLowerCase()) {
      if (char === wanted[index]) index++
    }
    return index === wanted.length
  })
}

export function slashQuery(text: string, start: number, end = start) {
  if (start !== end) return null
  const slash = text.lastIndexOf('/', start - 1)
  if (slash < 0 || slash >= start) return null
  const query = text.slice(slash + 1, start)
  return /\s/u.test(query) ? null : { start: slash, end: start, query }
}

// All content is plain text except atomic skill nodes. Never serialize their SVG/icon.
export function editorText(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? ''
  if (node instanceof HTMLElement) {
    if (node.dataset.skillRaw !== undefined) return node.dataset.skillRaw
    if (node.tagName === 'BR') return node.dataset.tail !== undefined ? '' : '\n'
  }
  return Array.from(node.childNodes).map(child => editorText(child)).join('')
}

export function copySkillText(event: { clipboardData: DataTransfer | null; preventDefault(): void }) {
  const selection = window.getSelection()
  if (!event.clipboardData || !selection?.rangeCount || selection.isCollapsed) return
  event.clipboardData.setData('text/plain', editorText(selection.getRangeAt(0).cloneContents()))
  event.preventDefault()
}

// A static message need not own focus when the browser dispatches a copy event.
export function copySelectedSkillText(event: ClipboardEvent) {
  if (event.defaultPrevented) return
  const selection = window.getSelection()
  if (!selection?.rangeCount) return
  if (selection.getRangeAt(0).cloneContents().querySelector('[data-skill-raw]')) copySkillText(event)
}
