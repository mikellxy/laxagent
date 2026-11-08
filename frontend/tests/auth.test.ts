import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { bootstrap, validToken } from '../src/features/auth/session'
import { clearCookie, readCookie, writeCookie } from '../src/lib/cookies'
import { json } from '../src/api/http'

const owner = '12345678-1234-4234-8234-123456789abc'
const jwt = (expires: number) => `header.${btoa(JSON.stringify({ sub: owner, exp: expires }))}.signature`
beforeEach(() => { clearCookie('user_id'); clearCookie('access_token') })
afterEach(() => vi.unstubAllGlobals())
it('loads a valid cookie session without logging in again', async () => {
  writeCookie('user_id', owner); writeCookie('access_token', jwt(Date.now() / 1000 + 3600))
  const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
  expect(await bootstrap()).toBe(owner); expect(fetcher).not.toHaveBeenCalled()
})
it('rejects malformed tokens and tokens belonging to another user', () => {
  const fresh = jwt(Date.now() / 1000 + 3600)
  writeCookie('access_token', fresh.slice(0, fresh.lastIndexOf('.')))
  expect(validToken(owner)).toBe(false)
  writeCookie('access_token', fresh)
  expect(validToken('00000000-0000-4000-8000-000000000000')).toBe(false)
})
it('refreshes expired tokens and leaves absent identity on the guest screen', async () => {
  expect(await bootstrap()).toBeNull()
  writeCookie('user_id', owner); writeCookie('access_token', jwt(1))
  const fresh = jwt(Date.now() / 1000 + 3600)
  const fetcher = vi.fn().mockResolvedValue(Response.json({ access_token: fresh, expires_in: 86400, initial_session_id: null }))
  vi.stubGlobal('fetch', fetcher)
  expect(await bootstrap()).toBe(owner)
  expect(readCookie('access_token')).toBe(fresh)
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ user_id: owner })
})
it('coalesces concurrent 401 refreshes and retries each request once', async () => {
  writeCookie('user_id', owner); writeCookie('access_token', 'old')
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/api/v1/login') return Response.json({ access_token: 'fresh', expires_in: 86400, initial_session_id: null })
    return new Headers(init?.headers).get('Authorization') === 'Bearer fresh' ? Response.json([]) : Response.json({ detail: 'expired' }, { status: 401 })
  })
  vi.stubGlobal('fetch', fetcher)
  expect(await Promise.all([json('/projects'), json('/models')])).toEqual([[], []])
  expect(fetcher.mock.calls.filter(c => c[0] === '/api/v1/login')).toHaveLength(1)
})
it('never retries a chat after a transport failure', async () => {
  const fetcher = vi.fn().mockRejectedValue(new TypeError('network error')); vi.stubGlobal('fetch', fetcher)
  await expect(json('/sessions/1/chat', { method: 'POST', body: '{}' })).rejects.toThrow('network error')
  expect(fetcher).toHaveBeenCalledOnce()
})
