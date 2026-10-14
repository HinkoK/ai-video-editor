// Помощники экрана «Расшифровка»: только показ, без арифметики стыков.
import type { Asset, Job, Segment, TranscriptInfo } from './types'

/** Время фразы для показа и перехода: начало речи, если известно. */
export function segmentStart(s: Segment): number {
  return s.speech_start_us ?? s.src_start_us
}

/** Фраза, которая звучит в момент us (в паузе — последняя прозвучавшая).
 *  Фразы идут по времени. -1 — момент раньше первой фразы. */
export function segmentIndexAt(segments: Segment[], us: number): number {
  let lo = 0
  let hi = segments.length - 1
  let found = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (segments[mid].src_start_us <= us) {
      found = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return found
}

/** Насколько точно время расшифровки — словами для строки над списком. */
export function timingText(t: Pick<TranscriptInfo, 'word_timing' | 'timing_level'>): string {
  if (t.word_timing === 'estimated') {
    return 'по словам, приблизительно (DTW): точные правки по словам возможны, но проверяйте на слух'
  }
  return t.timing_level === 'segment' ? 'по фразам, приблизительно: резать по словам нельзя' : 'по словам, проверенная'
}

/** Есть ли готовая лёгкая копия файла. Как на сервере: есть завершённая
 *  задача proxy этого файла. */
export function hasProxy(jobs: Job[], assetId: string): boolean {
  return jobs.some((j) => j.type === 'proxy' && j.asset_id === assetId && j.state === 'completed')
}

export interface TextPart {
  text: string
  hit: boolean
}

// ё и е сервер не различает; для сравнения сводим к одному виду той же длины
const fold = (s: string) => s.toLowerCase().replaceAll('ё', 'е')
const WORD_CHAR = /[\p{L}\p{N}]/u

/** Текст фразы с отмеченными совпадениями. Поиск сервера — по началам слов
 *  без учёта регистра, поэтому и здесь слово запроса ищется только в начале
 *  слова текста. */
export function highlight(text: string, query: string): TextPart[] {
  const words = fold(query).replaceAll('"', ' ').split(/\s+/).filter(Boolean)
  const hay = fold(text)
  // Если перевод в нижний регистр поменял длину, места совпадений не найти
  if (!words.length || hay.length !== text.length) return [{ text, hit: false }]
  const marked = new Array<boolean>(text.length).fill(false)
  for (const w of words) {
    for (let at = hay.indexOf(w); at >= 0; at = hay.indexOf(w, at + 1)) {
      if (at > 0 && WORD_CHAR.test(hay[at - 1])) continue
      marked.fill(true, at, at + w.length)
    }
  }
  const parts: TextPart[] = []
  for (let i = 0; i < text.length; i++) {
    const last = parts[parts.length - 1]
    if (last && last.hit === marked[i]) last.text += text[i]
    else parts.push({ text: text[i], hit: marked[i] })
  }
  return parts.length ? parts : [{ text, hit: false }]
}

export interface HitGroup {
  asset: Asset
  /** Номер файла в проекте, с единицы (A1, A2…) */
  number: number
  hits: Segment[]
}

/** Найденные фразы по файлам: файлы в порядке проекта, фразы по времени. */
export function groupHits(hits: Segment[], assets: Asset[]): HitGroup[] {
  return assets
    .map((asset, i) => ({
      asset,
      number: i + 1,
      hits: hits.filter((h) => h.asset_id === asset.id).sort((a, b) => a.src_start_us - b.src_start_us),
    }))
    .filter((g) => g.hits.length > 0)
}
