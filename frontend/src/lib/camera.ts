// Камера, снятая одновременно с экраном: как сказать человеку, где она
// относительно записи экрана. Время камеры = смещение + время записи ×
// (1 + уплывание / 1 000 000).
import type { Asset } from './types'

export const SYNC_STATE: Record<NonNullable<Asset['sync_state']>, string> = {
  pending: 'сводится по звуку',
  ok: 'сведена по звуку',
  weak: 'сведена неуверенно — проверьте на глаз',
  failed: 'не сведена: звук не совпал с записью экрана',
  manual: 'подстроена вручную',
}

const num = (v: number, digits: number) => v.toFixed(digits).replace('.', ',').replace('-', '−')

/** «камера раньше на 1,50 с» или «сдвиг камеры +21,3 мс». */
export function syncOffsetText(offsetUs: number): string {
  if (Math.abs(offsetUs) >= 1_000_000) {
    return `камера включена на ${num(Math.abs(offsetUs) / 1e6, 2)} с ${offsetUs > 0 ? 'раньше' : 'позже'}`
  }
  return `сдвиг камеры ${offsetUs >= 0 ? '+' : ''}${num(offsetUs / 1000, 1)} мс`
}

/** Уплывание часов в миллисекундах за час записи. */
export function driftText(ppm: number): string {
  const msPerHour = ppm * 3.6
  return Math.abs(msPerHour) < 0.05 ? 'уплывания нет' : `уплывает ${num(msPerHour, 1)} мс за час`
}

/** Одна строка для таблицы файлов. */
export function syncSummary(asset: Asset, assets: Asset[]): string {
  if (asset.role !== 'camera') return ''
  const parts: string[] = [SYNC_STATE[asset.sync_state ?? 'pending']]
  const main = assets.findIndex((a) => a.id === asset.sync_to)
  if (main >= 0) parts.push(`с A${main + 1}`)
  if (asset.sync_offset_us !== null && asset.sync_state !== 'failed') {
    parts.push(syncOffsetText(asset.sync_offset_us))
    if (asset.sync_drift_ppm !== null) parts.push(driftText(asset.sync_drift_ppm))
  }
  const points = asset.sync?.points ?? []
  if (points.length) parts.push(`точек: ${points.filter((p) => p.used).length} из ${points.length}`)
  return parts.join(' · ')
}

/** Сведённая камера записи экрана — та же, что берёт сервер при сборке:
 *  первая по порядку среди сведённых с этой записью. */
export function syncedCamera(assets: Asset[], mainId: string): Asset | undefined {
  return assets
    .filter((a) => a.role === 'camera' && a.sync_to === mainId && a.sync_offset_us !== null &&
      (a.sync_state === 'ok' || a.sync_state === 'weak' || a.sync_state === 'manual'))
    .sort((a, b) => a.position - b.position)[0]
}

/** Время камеры в момент записи экрана; undefined — камера тогда не снимала. */
export function cameraUs(camera: Asset, mainUs: number): number | undefined {
  const us = (camera.sync_offset_us ?? 0) + mainUs * (1 + (camera.sync_drift_ppm ?? 0) / 1e6)
  return us >= 0 && us < camera.duration_us ? us : undefined
}
