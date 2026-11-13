import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { SkillComposer } from '../src/features/chat/SkillComposer'
import { SkillText } from '../src/features/chat/SkillText'
import { editorText, matchesSkill, skillMarker, skillParts, slashQuery } from '../src/features/chat/skill-text'
import { api } from '../src/api/resources'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

test('matching is case insensitive ordered subsequence within either field', () => {
  const skill = { name: 'abc', desc: 'XyZ' }
  expect(matchesSkill(skill, '')).toBe(true)
  expect(matchesSkill(skill, 'ac')).toBe(true)
  expect(matchesSkill(skill, 'xz')).toBe(true)
  expect(matchesSkill(skill, 'cx')).toBe(false)
  expect(matchesSkill(skill, 'ca')).toBe(false)
  expect(matchesSkill(skill, 'aa')).toBe(false)
  expect(matchesSkill({ name: 'x', desc: '帮助创建技能' }, '帮技')).toBe(true)
})

test('marker round trip and arbitrary slash positions', () => {
  const text = `before${skillMarker('grilling')}${skillMarker('manual')}after\n`
  expect(skillParts(text).map(part => part.text).join('')).toBe(text)
  expect(skillParts(text).filter(part => part.name).map(part => part.name)).toEqual(['grilling', 'manual'])
  expect(skillParts('用户指定使用技能grilling')).toEqual([{ text: '用户指定使用技能grilling' }])
  expect(slashQuery('a/b', 3)).toEqual({ start: 1, end: 3, query: 'b' })
  for (const whitespace of [' ', '\n', '\t', '\u3000']) expect(slashQuery(`/x${whitespace}`, 3)).toBeNull()
  expect(slashQuery('/x', 1, 2)).toBeNull()
})

test('historical markers render independently of the current catalog and preserve copy source', () => {
  const { container } = render(<SkillText text={`hello${skillMarker('deleted-skill')}end`} />)
  expect(screen.getByLabelText('技能 deleted-skill')).toHaveTextContent('deleted-skill')
  expect(editorText(container)).toBe(`hello${skillMarker('deleted-skill')}end`)
})

test('picker filters locally, selects without sending, and deletes a token atomically', async () => {
  const list = vi.spyOn(api, 'skills').mockResolvedValue({ skills: [
    { name: 'grilling', desc: 'Stress questions' }, { name: 'manual', desc: 'Manual' },
  ] })
  const sent = vi.fn(), changed = vi.fn()
  function Harness() {
    const [value, setValue] = useState('')
    return <SkillComposer value={value} onChange={text => { changed(text); setValue(text) }} onSend={sent} mobile={false} />
  }
  const user = userEvent.setup()
  render(<Harness />)
  const input = screen.getByRole('textbox', { name: '消息输入' })
  await user.type(input, '/gr')
  expect(await screen.findByRole('option', { name: 'grilling Stress questions' })).toHaveAttribute('aria-selected', 'true')
  expect(screen.queryByRole('option', { name: 'manual Manual' })).not.toBeInTheDocument()
  expect(list).toHaveBeenCalledTimes(1)
  await user.keyboard('{Enter}')
  expect(sent).not.toHaveBeenCalled()
  expect(changed).toHaveBeenLastCalledWith(skillMarker('grilling'))
  expect(input.querySelector('[data-skill-raw]')).toHaveTextContent('grilling')
  await user.keyboard('{Backspace}')
  expect(changed).toHaveBeenLastCalledWith('')
})
