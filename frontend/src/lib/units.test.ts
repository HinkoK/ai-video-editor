import { describe, expect, it } from 'vitest'
import { baseName, capitalize, fmtBytes, fmtClock, fmtFps, fmtShortDate, groupDigits, shortPath } from './units'

describe('показ размеров и времени', () => {
  it('размер — как в макете, через запятую', () => {
    expect(fmtBytes(0)).toBe('0 МБ')
    expect(fmtBytes(500)).toBe('< 1 МБ')
    expect(fmtBytes(860 * 1024 ** 2)).toBe('860 МБ')
    expect(fmtBytes(4.2 * 1024 ** 3)).toBe('4,2 ГБ')
    // 1023,6 МБ не превращаются в «1024 МБ»
    expect(fmtBytes(1023.6 * 1024 ** 2)).toBe('1,0 ГБ')
  })

  it('разряды через неразрывный пробел', () => {
    expect(groupDigits(71060)).toBe('71 060')
    expect(groupDigits(3281678)).toBe('3 281 678')
    expect(groupDigits(999)).toBe('999')
    expect(groupDigits(-1500)).toBe('−1 500')
  })

  it('длительность файла целыми секундами', () => {
    expect(fmtClock(42_999_999)).toBe('00:42')
    expect(fmtClock((42 * 60 + 10) * 1_000_000)).toBe('42:10')
    expect(fmtClock((3600 + 2 * 60 + 5) * 1_000_000)).toBe('1:02:05')
  })

  it('частота кадров', () => {
    expect(fmtFps('30000/1001')).toBe('29,97 к/с')
    expect(fmtFps('24000/1001')).toBe('23,98 к/с')
    expect(fmtFps('25/1')).toBe('25 к/с')
    expect(fmtFps('30')).toBe('30 к/с')
    expect(fmtFps('0/0')).toBe('0/0')
  })

  it('дата — день, месяц, время по местному', () => {
    expect(fmtShortDate(new Date(2026, 9, 7, 22, 40).toISOString())).toBe('7 окт, 22:40')
    expect(fmtShortDate(new Date(2026, 4, 1, 9, 5).toISOString())).toBe('1 мая, 09:05')
    expect(fmtShortDate('не дата')).toBe('не дата')
  })

  it('имя файла и заглавная буква', () => {
    expect(baseName('/Users/me/Видео/камера.mp4')).toBe('камера.mp4')
    expect(baseName('камера.mp4')).toBe('камера.mp4')
    expect(shortPath('/Users/me/Видео/камера.mp4')).toBe('~/Видео/камера.mp4')
    expect(shortPath('/home/me/v.mp4')).toBe('~/v.mp4')
    expect(shortPath('/Volumes/Диск/v.mp4')).toBe('/Volumes/Диск/v.mp4')
    expect(capitalize('готовые ролики')).toBe('Готовые ролики')
    expect(capitalize('')).toBe('')
  })
})
