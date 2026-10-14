import { describe, expect, it } from 'vitest'
import { around, cutWindow, framesOf } from './frames'
import { parseRate } from './time'
import type { Revision } from './types'

// 25 к/с, кадр 40 мс. Запись A: 0–4 с — кадры 0–100, 10–14 с — кадры 100–200 (4–10 с вырезано)
const rate = parseRate('25/1')
const rev = {
  total_frames: 200,
  clips: [
    { id: 'a', asset_id: 'A', src_start_us: 0, src_end_us: 4_000_000, out_start_frame: 0, out_end_frame: 100 },
    { id: 'b', asset_id: 'A', src_start_us: 10_000_000, src_end_us: 14_000_000, out_start_frame: 100, out_end_frame: 200 },
  ],
} as unknown as Revision

describe('окна «До» и «После» на экране проверки', () => {
  it('отрезок записи в кадрах версии — от первого оставленного куска до последнего', () => {
    expect(framesOf(rev, rate, 'A', 1_000_000, 2_000_000)).toEqual([25, 50])
    // Через вырезанное: начало в первом куске, конец во втором
    expect(framesOf(rev, rate, 'A', 3_000_000, 11_000_000)).toEqual([75, 125])
    // Начало вырезано — с начала следующего оставленного куска
    expect(framesOf(rev, rate, 'A', 6_000_000, 12_000_000)).toEqual([100, 150])
    // Целиком вырезано, другой файл
    expect(framesOf(rev, rate, 'A', 5_000_000, 9_000_000)).toBeUndefined()
    expect(framesOf(rev, rate, 'B', 1_000_000, 2_000_000)).toBeUndefined()
  })
  it('окно с запасом не выходит за версию', () => {
    expect(around(rev, rate, 10, 20)).toEqual({ from: 0, to: 45 })
    expect(around(rev, rate, 150, 190, { mark: [150, 190] })).toEqual({ from: 125, to: 200, mark: [150, 190] })
    expect(around(rev, rate, 100, 100, {}, 3_000_000)).toEqual({ from: 25, to: 175 })
  })
  it('после перестановки части отрезка в разных местах ролика — та, где его начало', () => {
    // Запись A: 0–4 с (кадры 0–100), кусок 10–12 с переставлен в начало (кадры 100–150 → 0–50)
    const moved = {
      total_frames: 200,
      clips: [
        { id: 'm', asset_id: 'A', src_start_us: 10_000_000, src_end_us: 12_000_000, out_start_frame: 0, out_end_frame: 50 },
        { id: 'a', asset_id: 'A', src_start_us: 0, src_end_us: 4_000_000, out_start_frame: 50, out_end_frame: 150 },
        { id: 'b', asset_id: 'A', src_start_us: 12_000_000, src_end_us: 14_000_000, out_start_frame: 150, out_end_frame: 200 },
      ],
    } as unknown as Revision
    // Кусок 10–12 с — на новом месте
    expect(framesOf(moved, rate, 'A', 10_000_000, 12_000_000)).toEqual([0, 50])
    // 3–13 с: части 125–150 (с началом) и 150–175 встык в ролике — одним отрезком; 0–50 — отдельно
    expect(framesOf(moved, rate, 'A', 3_000_000, 13_000_000)).toEqual([125, 175])
    // 11–13 с: 25–50 и 150–175 не встык — часть с началом
    expect(framesOf(moved, rate, 'A', 11_000_000, 13_000_000)).toEqual([25, 50])
    // Соседние в ролике части — одним отрезком
    expect(framesOf(moved, rate, 'A', 11_000_000, 12_000_000)).toEqual([25, 50])
  })
  it('вырез: есть в версии — пунктир, нет — черта стыка', () => {
    // «До»: вырезаемое ещё в версии
    expect(cutWindow(rev, rate, 'A', 1_000_000, 2_000_000, 1_000_000)).toEqual({ from: 0, to: 75, mark: [25, 50] })
    // «После»: вырезанного нет — стык на начале следующего куска
    expect(cutWindow(rev, rate, 'A', 5_000_000, 9_000_000, 1_000_000)).toEqual({ from: 75, to: 125, joint: 100 })
    // После конца записи — показывать нечего
    expect(cutWindow(rev, rate, 'A', 15_000_000, 16_000_000, 1_000_000)).toBeUndefined()
  })
})
