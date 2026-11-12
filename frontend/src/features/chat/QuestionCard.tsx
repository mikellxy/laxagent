import { useState } from 'react'
import { PenLine, X } from 'lucide-react'
import type { Answer, PendingQuestion } from '../../api/types'
import styles from './QuestionCard.module.css'

export function QuestionCard({ pending, submitting, onSubmit }: {
  pending: PendingQuestion; submitting: boolean; onSubmit: (answers: Answer[]) => void
}) {
  const [page, setPage] = useState(0)
  const [values, setValues] = useState(() => pending.questions.map(() => ''))
  const [choices, setChoices] = useState<(number | null)[]>(() => pending.questions.map(() => null))
  const question = pending.questions[page]
  const completed = values.filter(value => value.trim()).length
  function update(value: string, choice: number | null) {
    setValues(current => current.map((old, index) => index === page ? value : old))
    setChoices(current => current.map((old, index) => index === page ? choice : old))
  }
  function submit(skipAll = false) {
    onSubmit(pending.questions.map((item, index) => ({ question: item.question,
      answer: skipAll || !values[index].trim() ? '用户未回答' : values[index] })))
  }
  return <section className={styles.card} aria-label="回答问题" aria-busy={submitting}>
    <div className={styles.heading}>
      <div className={styles.tabs} role="tablist" aria-label="问题页签">
        {pending.questions.map((_, index) => <button type="button" key={index} role="tab" aria-selected={page === index}
          aria-controls={`question-panel-${pending.interrupt_id}`} disabled={submitting} onClick={() => setPage(index)}>
          问题 {index + 1}{values[index].trim() ? ' ✓' : ''}
        </button>)}
      </div>
      <div className={styles.progress}>
        <span>已回答 {completed}/{pending.questions.length} · 第 {page + 1}/{pending.questions.length} 页</span>
        <button className={styles.close} type="button" aria-label="跳过全部问题" title="跳过全部问题并继续" disabled={submitting} onClick={() => submit(true)}><X size={15} /></button>
      </div>
    </div>
    <div role="tabpanel" id={`question-panel-${pending.interrupt_id}`}>
      <p className={styles.question}>{question.question}</p>
      <div className={styles.choices}>
        {question.choice.map((choice, index) => <button type="button" key={index} aria-pressed={choices[page] === index}
          disabled={submitting} onClick={() => update(choices[page] === index ? '' : choice, choices[page] === index ? null : index)}>{choice}</button>)}
      </div>
      <div className={styles.composer}>
        <PenLine size={15} className={styles.pen} aria-hidden />
        <input aria-label="自填答案" placeholder="或自行撰写回复" disabled={submitting}
          value={choices[page] === null ? values[page] : ''} onChange={event => update(event.target.value, null)} />
        <div className={styles.actions}>
          {page > 0 && <button type="button" disabled={submitting} onClick={() => setPage(page - 1)}>上一题</button>}
          <button type="button" disabled={submitting} onClick={() => update('', null)}>跳过</button>
          {page < pending.questions.length - 1
            ? <button type="button" className={styles.primary} disabled={submitting} onClick={() => setPage(page + 1)}>下一题</button>
            : <button type="button" className={styles.primary} disabled={submitting} onClick={() => submit()}>{submitting ? '正在提交…' : '提交'}</button>}
        </div>
      </div>
    </div>
  </section>
}

export function AnswerCard({ answers }: { answers: Answer[] }) {
  return <article className={styles.answers} aria-label="问答记录">
    {answers.map((item, index) => <div key={index}><p>{item.question}</p><div>{item.answer}</div></div>)}
  </article>
}
