// Время только для показа. Вся арифметика стыков и границ — на сервере;
// здесь лишь перевод кадров и микросекунд в «мм:сс.сс» и обратно для ввода.

export interface Rate {
  num: number
  den: number
}

export function parseRate(text: string): Rate {
  const [num, den = '1'] = text.split('/')
  const r = { num: Number(num), den: Number(den) }
  if (!(r.num > 0 && r.den > 0)) throw new Error(`не частота кадров: ${text}`)
  return r
}

/** Начало кадра в микросекундах (как frame_to_us на сервере). */
export function frameToUs(frame: number, rate: Rate): number {
  return Math.floor((frame * 1_000_000 * rate.den) / rate.num + 0.5)
}

/** Ближайшая к моменту граница кадра (как us_to_frame на сервере). */
export function usToFrame(us: number, rate: Rate): number {
  return Math.floor((us * rate.num) / (rate.den * 1_000_000) + 0.5)
}

/** Кадр, видимый в момент us (как frame_at на сервере). Не то же самое,
 *  что граница: начало кадра 2 при 29,97, округлённое до микросекунды,
 *  чуть раньше точного, и в этот момент ещё виден кадр 1. */
export function frameAt(us: number, rate: Rate): number {
  return Math.floor((us * rate.num) / (rate.den * 1_000_000))
}

/** «мм:сс.сс» (или «ч:мм:сс.сс»); digits — знаков после точки: в списках
 *  дизайн показывает десятые, при сдвиге стыков нужны сотые. */
export function fmtUs(us: number | null | undefined, digits: 1 | 2 = 2): string {
  if (us === null || us === undefined || !Number.isFinite(us)) return '—'
  const sign = us < 0 ? '−' : ''
  const unit = 10 ** digits
  // Округляем один раз, иначе 59,96 с дало бы «00:60.0»
  const s = Math.round((Math.abs(us) / 1_000_000) * unit) / unit
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = (s % 60).toFixed(digits).padStart(3 + digits, '0')
  return h > 0 ? `${sign}${h}:${String(m).padStart(2, '0')}:${sec}` : `${sign}${String(m).padStart(2, '0')}:${sec}`
}

/** «мм:сс.с», «чч:мм:сс» или секунды -> микросекунды. null, если не разобрать. */
export function parseTime(text: string): number | null {
  const parts = text.trim().replace(',', '.').split(':')
  if (parts.length === 0 || parts.length > 3 || parts.some((x) => x === '' || Number.isNaN(Number(x)))) return null
  const seconds = parts.reduce((acc, x) => acc * 60 + Number(x), 0)
  return seconds >= 0 ? Math.round(seconds * 1_000_000) : null
}
