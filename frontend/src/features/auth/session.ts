import { readCookie, writeCookie } from '../../lib/cookies'
import { responseError } from '../../api/errors'
import type { LoginResponse } from '../../api/types'

export function userId(): string | null {
  const value = readCookie('user_id')
  return value && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value) ? value.toLowerCase() : null
}
export function token() { return readCookie('access_token') }
export function validToken(owner: string) {
  try {
    const parts = token()!.split('.')
    if (parts.length !== 3 || parts.some(part => !part)) return false
    const segment = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const payload = JSON.parse(atob(segment))
    return payload.sub === owner && typeof payload.exp === 'number' && payload.exp * 1000 > Date.now()
  } catch { return false }
}
let refreshFlight: Promise<LoginResponse> | null = null
async function login(owner: string, guest: boolean): Promise<LoginResponse> {
  const response = await fetch('/api/v1/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: owner, ...(guest ? { from_page: 'guest_login' } : {}) }) })
  if (!response.ok) throw await responseError(response)
  const result: LoginResponse = await response.json()
  writeCookie('user_id', owner)
  writeCookie('access_token', result.access_token, result.expires_in)
  return result
}
export function refreshToken() {
  const owner = userId()
  if (!owner) return Promise.reject(new Error('请先登录'))
  if (!refreshFlight) refreshFlight = login(owner, false).finally(() => { refreshFlight = null })
  return refreshFlight
}
export async function bootstrap() {
  const owner = userId()
  if (owner && !validToken(owner)) await refreshToken()
  return owner
}
let guestId: string | null = null
export async function guestLogin() {
  guestId ??= crypto.randomUUID()
  const result = await login(guestId, true)
  return { owner: guestId, session: result.initial_session_id }
}
