import { refreshToken, token } from '../features/auth/session'
import { responseError } from './errors'

export async function request(path: string, init: RequestInit = {}): Promise<Response> {
  const usedToken = token()
  const send = () => fetch(`/api/v1${path}`, { ...init, headers: {
    ...(init.body ? { 'Content-Type': 'application/json' } : {}),
    ...init.headers, ...(token() ? { Authorization: `Bearer ${token()}` } : {}),
  } })
  let response = await send()
  if (response.status === 401) {
    // A second request may have already refreshed the token while this one was in flight.
    if (token() === usedToken) await refreshToken()
    init.signal?.throwIfAborted()
    response = await send()
  }
  if (!response.ok) throw await responseError(response)
  return response
}
export async function json<T>(path: string, init: RequestInit = {}): Promise<T> {
  return (await request(path, init)).json()
}
