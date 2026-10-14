import { describe, expect, it } from 'vitest'
import { blockOf, editPoints, keyAction, mergeRanges, moveOp, nextPoint, pieceAt, prevPoint, rangeOps, shuttle, trimToNext, trimToPrev, whereInVersion } from './edit'
import { parseRate } from './time'
import type { Revision } from './types'

const key = (code: string, mods: Partial<Record<'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey', boolean>> = {}) =>
  keyAction({ code, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...mods })

describe('ручной монтаж как в Premiere', () => {
  const points = editPoints(300, [100, 200], [150])
  it('точки и куски', () => {
    expect(points).toEqual([0, 100, 150, 200, 300])
    expect(pieceAt(points, 120)).toEqual([100, 150])
    expect(pieceAt(points, 150)).toEqual([150, 200])
    expect(pieceAt(points, 300)).toBeUndefined()
    expect(prevPoint(points, 150)).toBe(100)
    expect(nextPoint(points, 150)).toBe(200)
  })
  it('Q и W — до курсора от соседней точки', () => {
    expect(trimToPrev(points, 170)).toEqual([150, 170])
    expect(trimToNext(points, 170)).toEqual([170, 200])
    // Курсор на точке: Q берёт кусок слева, W — справа
    expect(trimToPrev(points, 150)).toEqual([100, 150])
    expect(trimToNext(points, 150)).toEqual([150, 200])
    expect(trimToPrev(points, 0)).toBeUndefined()
  })
  it('отрезки сливаются', () => {
    expect(mergeRanges([[50, 60], [10, 20], [20, 30], [55, 70], [5, 5]])).toEqual([[10, 30], [50, 70]])
  })
  it('отрезок ролика — вырезы исходника по кускам версии', () => {
    const rev = {
      clips: [
        { id: 'a', asset_id: 'A', src_start_us: 0, src_end_us: 4_000_000, out_start_frame: 0, out_end_frame: 100 },
        { id: 'b', asset_id: 'A', src_start_us: 10_000_000, src_end_us: 14_000_000, out_start_frame: 100, out_end_frame: 200 },
      ],
    } as unknown as Revision
    // 25 к/с: кадр 40 мс. Отрезок через стык — два выреза
    expect(rangeOps(rev, parseRate('25/1'), 75, 125, 'q').map((o) => [o.src_start_us, o.src_end_us])).toEqual([
      [3_000_000, 4_000_000], [10_000_000, 11_000_000]])
    expect(rangeOps(rev, parseRate('25/1'), 200, 250, 'q')).toEqual([])
  })
  it('клавиши Premiere — и на русской раскладке (по коду клавиши)', () => {
    expect(key('KeyK', { metaKey: true })).toBe('addEdit')
    expect(key('KeyK')).toBe('stop')
    expect(key('KeyZ', { metaKey: true })).toBe('undo')
    expect(key('KeyZ', { metaKey: true, shiftKey: true })).toBe('redo')
    expect(key('KeyQ')).toBe('trimPrev')
    expect(key('KeyW')).toBe('trimNext')
    expect(key('Quote')).toBe('extract')
    expect(key('Backspace', { shiftKey: true })).toBe('delete')
    expect(key('Backspace', { altKey: true })).toBe('delete')
    expect(key('KeyX', { altKey: true })).toBe('clearInOut')
    expect(key('ArrowUp')).toBe('prevEdit')
    expect(key('ArrowRight', { shiftKey: true })).toBe('frameFwd5')
    expect(key('KeyC')).toBe('razor')
    expect(key('KeyS', { metaKey: true })).toBeUndefined()
  })
  it('вырезали кусок, где идёт видео, — просмотр продолжается со следующего оставленного', () => {
    const rev = {
      clips: [
        { id: 'a', asset_id: 'A', src_start_us: 0, src_end_us: 3_000_000, out_start_frame: 0, out_end_frame: 75 },
        { id: 'b', asset_id: 'A', src_start_us: 6_000_000, src_end_us: 24_000_000, out_start_frame: 75, out_end_frame: 525 },
      ],
    } as unknown as Revision
    const rate = parseRate('25/1')
    expect(whereInVersion(rev, rate, 'A', 2_000_000)).toEqual({ frame: 50, us: 2_000_000, jumped: false })
    // 4,5 с вырезано — следующий оставленный кусок с 6 с, в ролике кадр 75
    expect(whereInVersion(rev, rate, 'A', 4_500_000)).toEqual({ frame: 75, us: 6_000_000, jumped: true })
    expect(whereInVersion(rev, rate, 'A', 7_000_000)).toEqual({ frame: 100, us: 7_000_000, jumped: false })
    expect(whereInVersion(rev, rate, 'A', 25_000_000)).toBeUndefined()
  })
  it('после перестановки вырезанное место ведёт к следующему по записи, а не по ролику', () => {
    // Кусок 20–24 с переставлен в начало ролика; 3–6 с вырезано
    const rev = {
      clips: [
        { id: 'm', asset_id: 'A', src_start_us: 20_000_000, src_end_us: 24_000_000, out_start_frame: 0, out_end_frame: 100 },
        { id: 'a', asset_id: 'A', src_start_us: 0, src_end_us: 3_000_000, out_start_frame: 100, out_end_frame: 175 },
        { id: 'b', asset_id: 'A', src_start_us: 6_000_000, src_end_us: 20_000_000, out_start_frame: 175, out_end_frame: 525 },
      ],
    } as unknown as Revision
    const rate = parseRate('25/1')
    expect(whereInVersion(rev, rate, 'A', 4_000_000)).toEqual({ frame: 175, us: 6_000_000, jumped: true })
    expect(whereInVersion(rev, rate, 'A', 21_000_000)).toEqual({ frame: 25, us: 21_000_000, jumped: false })
  })
  it('J/L: каждое нажатие в ту же сторону — вдвое быстрее', () => {
    expect(shuttle(0, 1)).toBe(1)
    expect(shuttle(1, 1)).toBe(2)
    expect(shuttle(8, 1)).toBe(8)
    expect(shuttle(2, -1)).toBe(-1)
    expect(shuttle(-1, -1)).toBe(-2)
  })

  it('перестановка: ⌘X — взять, ⌘⇧V — вставить со сдвигом, ⌘V — подсказка, ⌘C не трогаем', () => {
    expect(key('KeyX', { metaKey: true })).toBe('take')
    expect(key('KeyV', { metaKey: true, shiftKey: true })).toBe('insertShift')
    expect(key('KeyV', { ctrlKey: true, shiftKey: true })).toBe('insertShift')
    expect(key('KeyV', { metaKey: true })).toBe('paste')
    expect(key('KeyC', { metaKey: true })).toBeUndefined()
    expect(key('KeyV')).toBe('select')
    expect(key('KeyX', { altKey: true })).toBe('clearInOut')
    expect(key('Escape')).toBe('escape')
  })
  it('взять кусок: один отрезок записи, через вырез — тоже; из разных мест — нет', () => {
    const rate = parseRate('25/1')
    const rev = {
      clips: [
        { id: 'a', asset_id: 'A', src_start_us: 0, src_end_us: 4_000_000, out_start_frame: 0, out_end_frame: 100 },
        { id: 'b', asset_id: 'A', src_start_us: 10_000_000, src_end_us: 14_000_000, out_start_frame: 100, out_end_frame: 200 },
        { id: 'm', asset_id: 'A', src_start_us: 30_000_000, src_end_us: 32_000_000, out_start_frame: 200, out_end_frame: 250 },
        { id: 'c', asset_id: 'A', src_start_us: 20_000_000, src_end_us: 24_000_000, out_start_frame: 250, out_end_frame: 350 },
        { id: 'd', asset_id: 'B', src_start_us: 0, src_end_us: 4_000_000, out_start_frame: 350, out_end_frame: 450 },
      ],
    } as unknown as Revision
    // Кусок b целиком
    expect(blockOf(rev, rate, 100, 200)).toEqual({ asset_id: 'A', src_start_us: 10_000_000, src_end_us: 14_000_000 })
    // Через вырез 4–10 с: один отрезок записи 3–11 с
    expect(blockOf(rev, rate, 75, 125)).toEqual({ asset_id: 'A', src_start_us: 3_000_000, src_end_us: 11_000_000 })
    // Через переставленный кусок (30 с, потом 20 с) — из разных мест записи
    expect(blockOf(rev, rate, 150, 300)).toBeUndefined()
    // Отрезок записи 10–24 с задел бы и 20–24 с, стоящий в ролике отдельно
    expect(blockOf(rev, rate, 150, 250)).toBeUndefined()
    // Два файла
    expect(blockOf(rev, rate, 300, 400)).toBeUndefined()
    expect(moveOp({ asset_id: 'A', src_start_us: 1, src_end_us: 2 }, 40, 'вручную')).toEqual({
      kind: 'move', asset_id: 'A', src_start_us: 1, src_end_us: 2, reason: 'reorder', why: 'вручную', precise: true,
      target_edited_frame: 40, target_side: 'before',
    })
  })
})
