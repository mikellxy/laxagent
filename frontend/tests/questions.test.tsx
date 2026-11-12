import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QuestionCard, AnswerCard } from '../src/features/chat/QuestionCard'
import { groupTimeline } from '../src/features/chat/timeline'
import type { PendingQuestion, ToolMessage } from '../src/api/types'

afterEach(cleanup)

const pending: PendingQuestion = { run_id: 1, resume_time: 0, interrupt_id: 'id', tool_call_id: 'call', questions: [
  { question: '选择数据库？', choice: ['PostgreSQL', 'SQLite'] }, { question: '补充说明？', choice: [] },
] }

describe('question cards', () => {
  it('uses tabs, preserves drafts, allows custom answers and submits skipped questions', async () => {
    const user = userEvent.setup(), submit = vi.fn()
    render(<QuestionCard pending={pending} submitting={false} onSubmit={submit} />)
    expect(screen.queryByRole('button', { name: '提交答案' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'PostgreSQL' }))
    expect(screen.getByText('已回答 1/2 · 第 1/2 页')).toBeInTheDocument()
    await user.type(screen.getByRole('textbox'), '自己的方案')
    expect(screen.getByRole('button', { name: 'PostgreSQL' })).toHaveAttribute('aria-pressed', 'false')
    await user.click(screen.getByRole('button', { name: '下一题' }))
    await user.click(screen.getByRole('tab', { name: '问题 1 ✓' }))
    expect(screen.getByRole('textbox')).toHaveValue('自己的方案')
    await user.click(screen.getByRole('button', { name: '下一题' }))
    await user.click(screen.getByRole('button', { name: '提交答案' }))
    expect(submit).toHaveBeenCalledExactlyOnceWith([
      { question: '选择数据库？', answer: '自己的方案' }, { question: '补充说明？', answer: '用户未回答' },
    ])
  })
  it('closing submits every question as skipped, even after filling an answer', async () => {
    const user = userEvent.setup(), submit = vi.fn()
    render(<QuestionCard pending={pending} submitting={false} onSubmit={submit} />)
    await user.click(screen.getByRole('button', { name: 'SQLite' }))
    await user.click(screen.getByRole('button', { name: '跳过全部问题' }))
    expect(submit).toHaveBeenCalledWith(pending.questions.map(item => ({ question: item.question, answer: '用户未回答' })))
  })
  it('shows answers outside work groups and hides incomplete/error question tools', () => {
    const question: ToolMessage = { seq: 3, role: 'tool', tool_name: 'ask_user_question', arguments: '{}' }
    expect(groupTimeline([question], null)).toEqual([])
    const answered = { ...question, answers: [{ question: '选择数据库？', answer: 'SQLite' }] }
    expect(groupTimeline([answered], null)).toMatchObject([{ kind: 'message', message: answered }])
    render(<AnswerCard answers={answered.answers} />)
    expect(screen.getByLabelText('问答记录')).toHaveTextContent('选择数据库？SQLite')
  })
})
