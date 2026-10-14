// Показ размеров, длительностей, частоты кадров и дат на экранах «Проект»,
// «Экспорт» и «Задачи». Только форматирование: арифметика времени монтажа
// — в time.ts и на сервере.

const MB = 1024 ** 2
const GB = 1024 ** 3

/** «4,2 ГБ», «860 МБ», «< 1 МБ», «0 МБ» (как в макете, через запятую). */
export function fmtBytes(bytes: number): string {
  if (bytes >= 1000 * MB) return `${(bytes / GB).toFixed(1).replace('.', ',')} ГБ`
  if (bytes >= MB) return `${Math.round(bytes / MB)} МБ`
  return bytes > 0 ? '< 1 МБ' : '0 МБ'
}

/** Разряды через неразрывный пробел: 71 060. */
export function groupDigits(n: number): string {
  const s = String(Math.trunc(Math.abs(n))).replace(/\B(?=(\d{3})+(?!\d))/g, ' ')
  return n < 0 ? `−${s}` : s
}

/** Длительность файла целыми секундами: «42:10», «1:02:05». */
export function fmtClock(us: number): string {
  const total = Math.max(0, Math.floor(us / 1_000_000))
  const h = Math.floor(total / 3600)
  const mm = String(Math.floor((total % 3600) / 60)).padStart(2, '0')
  const ss = String(total % 60).padStart(2, '0')
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`
}

/** Частота кадров «30000/1001» → «29,97 к/с», «25/1» → «25 к/с». */
export function fmtFps(rate: string): string {
  const [num, den = '1'] = rate.split('/')
  const value = Number(num) / Number(den)
  if (!Number.isFinite(value) || value <= 0) return rate
  return `${String(Math.round(value * 100) / 100).replace('.', ',')} к/с`
}

const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']

/** «7 окт, 22:40» по местному времени. */
export function fmtShortDate(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${d.getDate()} ${MONTHS[d.getMonth()]}, ${hh}:${mm}`
}

/** Имя файла из полного пути. */
export function baseName(path: string): string {
  return path.split(/[\\/]/).pop() || path
}

/** Путь с домашней папкой как «~/…»: короче, полный путь — в подсказке. */
export function shortPath(path: string): string {
  return path.replace(/^\/(?:Users|home)\/[^/]+(?=\/)/, '~')
}

/** Первая буква заглавная: названия с сервера приходят строчными. */
export function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1)
}
