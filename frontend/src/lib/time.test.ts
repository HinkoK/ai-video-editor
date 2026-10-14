import { describe, expect, it } from 'vitest'
import { fmtUs, frameAt, frameToUs, parseRate, parseTime, usToFrame } from './time'

const NTSC = parseRate('30000/1001')

describe('время', () => {
  it('частота кадров — точная дробь', () => {
    expect(parseRate('30000/1001')).toEqual({ num: 30000, den: 1001 })
    expect(parseRate('25')).toEqual({ num: 25, den: 1 })
    expect(() => parseRate('0/0')).toThrow()
  })

  it('кадры и микросекунды совпадают с сервером', () => {
    // frame_to_us(1, 30000/1001) на сервере = 33367
    expect(frameToUs(1, NTSC)).toBe(33367)
    expect(frameToUs(107892, NTSC)).toBe(3_599_996_400)
    for (const f of [0, 1, 2, 999, 1000, 107891]) expect(usToFrame(frameToUs(f, NTSC), NTSC)).toBe(f)
    // Граница кадра 2 округлена вниз до 66733 мкс: в этот момент виден ещё кадр 1
    expect(frameToUs(2, NTSC)).toBe(66733)
    expect(frameAt(66733, NTSC)).toBe(1)
    expect(frameAt(66734, NTSC)).toBe(2)
  })

  it('показ времени', () => {
    expect(fmtUs(0)).toBe('00:00.00')
    expect(fmtUs(62_500_000)).toBe('01:02.50')
    expect(fmtUs(3_723_400_000)).toBe('1:02:03.40')
    expect(fmtUs(null)).toBe('—')
    expect(fmtUs(724_300_000, 1)).toBe('12:04.3')
    // Округление один раз: 59,96 с — это минута, а не «00:60.0»
    expect(fmtUs(59_960_000, 1)).toBe('01:00.0')
    expect(fmtUs(59_996_000)).toBe('01:00.00')
  })

  it('ввод времени', () => {
    expect(parseTime('01:02.5')).toBe(62_500_000)
    expect(parseTime('1:00:00')).toBe(3_600_000_000)
    expect(parseTime('7,25')).toBe(7_250_000)
    expect(parseTime('abc')).toBeNull()
    expect(parseTime('1::2')).toBeNull()
  })
})
