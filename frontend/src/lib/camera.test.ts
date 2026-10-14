import { describe, expect, it } from 'vitest'
import { cameraUs, driftText, syncedCamera, syncOffsetText, syncSummary } from './camera'
import type { Asset } from './types'

const base = { position: 0, path: '/x/a.mp4', fps: '60/1', frame_count: 1, duration_us: 1, width: 1, height: 1 }

describe('камера относительно записи экрана', () => {
  it('сдвиг словами', () => {
    expect(syncOffsetText(21_250)).toBe('сдвиг камеры +21,3 мс')
    expect(syncOffsetText(1_500_000)).toBe('камера включена на 1,50 с раньше')
    expect(syncOffsetText(-12_479_000)).toBe('камера включена на 12,48 с позже')
  })
  it('уплывание за час', () => {
    expect(driftText(0)).toBe('уплывания нет')
    expect(driftText(80)).toBe('уплывает 288,0 мс за час')
  })
  it('строка для таблицы', () => {
    const main = { ...base, id: 'm', role: 'main', sync_to: null, sync_offset_us: null, sync_drift_ppm: null, sync_state: null, sync: null } as Asset
    const cam = {
      ...base, id: 'c', role: 'camera', sync_to: 'm', sync_offset_us: 21_250, sync_drift_ppm: 0, sync_state: 'ok',
      sync: { points: [{ main_us: 1, offset_us: 1, peak: 40, used: true }, { main_us: 2, offset_us: 1, peak: 3, used: false }],
        residual_us: 0, coarse_peak: 190, measured: { offset_us: 21_250, drift_ppm: 0, state: 'ok' } },
    } as Asset
    expect(syncSummary(cam, [main, cam])).toBe('сведена по звуку · с A1 · сдвиг камеры +21,3 мс · уплывания нет · точек: 1 из 2')
    expect(syncSummary(main, [main, cam])).toBe('')
  })
  it('какая камера и её время', () => {
    const main = { ...base, id: 'm', role: 'main', sync_to: null, sync_offset_us: null, sync_drift_ppm: null, sync_state: null, sync: null } as Asset
    const cam = (id: string, position: number, state: Asset['sync_state']) => ({
      ...base, id, position, role: 'camera', sync_to: 'm', sync_offset_us: 1_500_000, sync_drift_ppm: 100,
      sync_state: state, sync: null, duration_us: 10_000_000,
    }) as Asset
    expect(syncedCamera([main, cam('c2', 2, 'ok'), cam('c1', 1, 'weak'), cam('c0', 0, 'failed')], 'm')?.id).toBe('c1')
    expect(syncedCamera([main, cam('c0', 0, 'pending')], 'm')).toBeUndefined()
    const c = cam('c', 1, 'ok')
    expect(cameraUs(c, 1_000_000)).toBe(2_500_100)
    expect(cameraUs(c, 9_000_000)).toBeUndefined() // камера уже выключена
    expect(cameraUs({ ...c, sync_offset_us: -2_000_000 }, 1_000_000)).toBeUndefined() // ещё не включена
  })
})
