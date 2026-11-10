import { beforeEach, expect, it } from 'vitest'
import { chooseModel, forgetSession, selectSession, selectedSession, sessionModel } from '../src/lib/preferences'
import { clearCookie, readCookie, writeCookie } from '../src/lib/cookies'
import { errorText } from '../src/api/errors'
import { validToken } from '../src/features/auth/session'
const catalog = { default_model: 'p:a', models: ['a', 'b', 'c'].map(name => ({ id: `p:${name}`, provider: 'p', name })) }
beforeEach(() => { clearCookie('laxcode_preferences'); clearCookie('access_token') })
it('keeps per-session choices distinct from the global last action', () => {
  expect(sessionModel('u', 1, catalog)).toBe('p:a')
  chooseModel('u', 1, 'p:b')
  expect(sessionModel('u', 2, catalog)).toBe('p:b')
  chooseModel('u', 2, 'p:c')
  expect(sessionModel('u', 1, catalog)).toBe('p:b')
  expect(sessionModel('u', 3, catalog)).toBe('p:c')
  selectSession('u', 2)
  expect(selectedSession('u')).toBe(2)
  expect(selectedSession('other')).toBeNull()
})
it('falls back to the configured default for stale models and damaged cookies', () => {
  chooseModel('u', 1, 'removed:model')
  expect(sessionModel('u', 1, catalog)).toBe('p:a')
  writeCookie('laxcode_preferences', '{broken')
  expect(sessionModel('u', 2, catalog)).toBe('p:a')
})
it('bounds preference cookie size and retains recent session choices', () => {
  for (let i = 1; i <= 500; i++) chooseModel('u', i, 'p:b')
  expect(encodeURIComponent(readCookie('laxcode_preferences')!).length).toBeLessThanOrEqual(3500)
  expect(sessionModel('u', 500, catalog)).toBe('p:b')
})
it('normalizes both error shapes and validation arrays', () => {
  expect(errorText({ detail: { error: 'Database unavailable' } })).toBe('Database unavailable')
  expect(errorText({ detail: 'Not found' })).toBe('Not found')
  expect(errorText({ detail: [{ msg: 'Required' }, { msg: 'Invalid UUID' }] })).toBe('Required；Invalid UUID')
})
it('requires a matching subject and a future token expiration', () => {
  const jwt = (sub: string, exp: number) => `header.${btoa(JSON.stringify({ sub, exp }))}.signature`
  writeCookie('access_token', jwt('u', Date.now() / 1000 + 3600))
  expect(validToken('u')).toBe(true); expect(validToken('other')).toBe(false)
  writeCookie('access_token', jwt('u', 1)); expect(validToken('u')).toBe(false)
  writeCookie('access_token', 'broken'); expect(validToken('u')).toBe(false)
})

it('forgets only the deleted session and preserves the global model preference', () => {
  chooseModel('u', 1, 'p:b'); chooseModel('u', 2, 'p:c'); selectSession('u', 2)
  forgetSession('u', 1)
  expect(selectedSession('u')).toBe(2)
  expect(JSON.parse(readCookie('laxcode_preferences')!).models).toEqual([[2, 'p:c']])
  forgetSession('u', 2)
  expect(selectedSession('u')).toBeNull()
  expect(JSON.parse(readCookie('laxcode_preferences')!).models).toEqual([])
  expect(sessionModel('u', 3, catalog)).toBe('p:c')
})
