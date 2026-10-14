// Кадр и графика в очереди проверки: участки слоя кадра и моушн-графика
// одним видом элемента. Чистые функции.
import { shotTitle } from './shots'
import type { GraphicItem, VisualSegment } from './types'

export interface LayerItem {
  kind: 'shot' | 'graphic'
  id: string
  asset_id: string
  src_start_us: number
  src_end_us: number
  review: 'pending' | 'kept' | 'reverted'
  goal: string
  created_by: string
  edited_ranges: [number, number][]
  in_revision: boolean
  title: string
  details: string[]
}

const cut = (s: unknown, n = 48) => {
  const t = String(s ?? '')
  return t.length > n ? `${t.slice(0, n - 1)}…` : t
}

export function graphicTitle(template: string, props: Record<string, unknown>): string {
  const list = (v: unknown) => (Array.isArray(v) ? v.length : 0)
  switch (template) {
    case 'lower_third': return `Подпись: ${cut(props.title)}`
    case 'term': return `Термин: ${cut(props.term)}`
    case 'keyword': return `Акцент: ${cut(props.text)}`
    case 'chapter': return `Глава: ${cut(props.title)}`
    case 'steps': return `Схема этапов (${list(props.steps)})`
    case 'compare': {
      const l = (props.left as { title?: string } | undefined)?.title
      const r = (props.right as { title?: string } | undefined)?.title
      return `Сравнение: ${cut(l, 20)} / ${cut(r, 20)}`
    }
    case 'checklist': return `Чек-лист (${list(props.items)})`
    case 'command': return `Команда: ${cut(props.text, 40)}`
    case 'page_insert': return 'Вставка страницы'
    default: return template
  }
}

/** Текст без звёздочек акцента («это *важно*» → «это важно»). */
const plain = (v: unknown) => (typeof v === 'string' ? v.replace(/\*/g, '') : '')

/** Что именно будет на экране — строками, для карточки проверки. */
export function graphicDetails(template: string, props: Record<string, unknown>): string[] {
  const out: string[] = []
  const str = (v: unknown) => (typeof v === 'string' ? v : '')
  if (template === 'steps') {
    const steps = (props.steps as string[]) ?? []
    const active = typeof props.active === 'number' ? props.active : -1
    if (props.title) out.push(str(props.title))
    steps.forEach((s, i) => out.push(`${i + 1}. ${s}${i === active ? ' — сейчас' : i < active ? ' — пройден' : ''}`))
  } else if (template === 'compare') {
    for (const side of ['left', 'right'] as const) {
      const c = props[side] as { title?: string; items?: string[] } | undefined
      if (c) out.push(`${c.title}: ${(c.items ?? []).join('; ')}`)
    }
    if (props.note) out.push(str(props.note))
  } else if (template === 'checklist') {
    const mark: Record<string, string> = { done: 'сделано', now: 'сейчас', todo: 'впереди' }
    for (const it of (props.items as { text: string; state: string }[]) ?? []) out.push(`${it.text} — ${mark[it.state] ?? it.state}`)
  } else {
    for (const key of ['title', 'subtitle', 'term', 'definition', 'text', 'line1', 'line2', 'sub', 'caption', 'note', 'number']) {
      if (props[key]) out.push(plain(props[key]) || String(props[key]))
    }
  }
  return out
}

export function shotDetails(s: Pick<VisualSegment, 'params'>): string[] {
  const out: string[] = []
  const z = s.params.zoom
  if (z) {
    out.push(`область ${Math.round(z.x * 100)}–${Math.round((z.x + z.w) * 100)}% по ширине, ` +
      `${Math.round(z.y * 100)}–${Math.round((z.y + z.h) * 100)}% по высоте`)
  }
  if (s.params.size) out.push(`лицо — ${Math.round(s.params.size * 100)}% ширины кадра`)
  const p = s.params.punch
  if (p) {
    out.push(`лицо крупнее ×${p.scale}` + (p.release ? `, за ${p.release} с плавно назад к общему плану` : ''))
  }
  if (s.params.push) out.push(`медленный наезд на ${Math.round(s.params.push * 100)}% на кусках от 5 с`)
  if (s.params.alternate) {
    out.push(`план меняется на склейках: общий и крупнее на ${Math.round(s.params.alternate.step * 100)}%`)
  }
  if (s.params.drop) out.push(`экран опущен на ${Math.round(s.params.drop * 100)}% высоты — сверху освобождается место`)
  return out
}

export function fromShot(s: VisualSegment): LayerItem {
  return { ...s, kind: 'shot', title: shotTitle(s), details: shotDetails(s) }
}

export function fromGraphic(g: GraphicItem): LayerItem {
  return { ...g, kind: 'graphic', title: graphicTitle(g.template, g.props), details: graphicDetails(g.template, g.props) }
}
