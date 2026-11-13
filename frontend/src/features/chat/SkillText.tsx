import { skillParts, copySkillText } from './skill-text'
import styles from './Skills.module.css'

export function SkillText({ text }: { text: string }) {
  return <span onCopy={copySkillText}>{skillParts(text).map((part, index) => part.name
    ? <span key={index} className={styles.skill} data-skill-raw={part.text} aria-label={`技能 ${part.name}`}>{part.name}</span>
    : part.text)}</span>
}
