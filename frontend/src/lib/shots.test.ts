import { describe, expect, it } from 'vitest'
import { shotPlace, shotTitle } from './shots'
import type { Asset } from './types'

describe('участок кадра словами', () => {
  it('раскладки', () => {
    expect(shotTitle({ layout: 'screen', params: {} })).toBe('Экран без лица')
    expect(shotTitle({ layout: 'screen', params: { zoom: { x: 0, y: 0, w: 0.4, h: 0.4 } } })).toBe('Приближение экрана ×2,5')
    expect(shotTitle({ layout: 'camera', params: { punch: { scale: 1.15, cx: 0.5, cy: 0.45 } } })).toBe('Лицо целиком, крупнее ×1,15')
    expect(shotTitle({ layout: 'camera', params: {} })).toBe('Лицо целиком')
    expect(shotTitle({ layout: 'pip', params: { slot: 'tl', size: 0.2 } })).toBe('Экран с лицом в углу слева вверху')
  })
  it('место', () => {
    const a = { id: 'a1' } as Asset
    expect(shotPlace({ asset_id: 'a1', src_start_us: 10_000_000, src_end_us: 14_000_000 }, [a])).toBe('A1 00:10.0–00:14.0 · 4.0 с')
  })
})
