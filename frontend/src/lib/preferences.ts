import { readCookie, writeCookie } from './cookies'
import type { Models } from '../api/types'

interface Preferences { owner: string; selected: number | null; global: string | null; models: [number, string][] }
function read(owner: string): Preferences {
  try {
    const p = JSON.parse(readCookie('laxcode_preferences') ?? 'null')
    if (p?.owner === owner) return {
      owner, selected: Number.isSafeInteger(p.selected) && p.selected > 0 ? p.selected : null,
      global: typeof p.global === 'string' ? p.global : null,
      models: Array.isArray(p.models) ? p.models.filter((v: unknown) => Array.isArray(v) && Number.isSafeInteger(v[0]) && v[0] > 0 && typeof v[1] === 'string') : [],
    }
  } catch { /* Invalid or old preferences are disposable. */ }
  return { owner, selected: null, global: null, models: [] }
}
function save(p: Preferences) {
  while (encodeURIComponent(JSON.stringify(p)).length > 3500 && p.models.length) p.models.shift()
  writeCookie('laxcode_preferences', JSON.stringify(p))
}
export function selectedSession(owner: string) { return read(owner).selected }
export function selectSession(owner: string, session: number | null) { const p = read(owner); p.selected = session; save(p) }
export function sessionModel(owner: string, session: number, catalog: Models): string {
  const p = read(owner)
  const selected = p.models.find(([id]) => id === session)?.[1] ?? p.global ?? catalog.default_model
  const result = catalog.models.some(m => m.id === selected) ? selected : catalog.default_model
  // Snapshot the initial choice for this session without changing the global last action.
  p.models = p.models.filter(([id]) => id !== session)
  p.models.push([session, result]); save(p)
  return result
}
export function chooseModel(owner: string, session: number, model: string) {
  const p = read(owner)
  p.global = model
  p.models = p.models.filter(([id]) => id !== session)
  p.models.push([session, model]); save(p)
}
