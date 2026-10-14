// Слой кадра для показа человеку: как назвать участок и где он. Чистые
// функции; данные — visual_segments сервера (backend/src/longform/visual.py).
import { assetRef } from './review'
import { fmtUs } from './time'
import type { Asset, VisualSegment } from './types'

const SLOT: Record<string, string> = { br: 'справа внизу', bl: 'слева внизу', tr: 'справа вверху', tl: 'слева вверху' }
const n = (v: number, digits = 2) => v.toFixed(digits).replace(/\.?0+$/, '').replace('.', ',')

export const SHOT_REVIEW: Record<VisualSegment['review'], string> = {
  pending: 'ещё не проверено',
  kept: 'оставлено',
  reverted: 'возвращено',
}

/** «Лицо целиком, крупнее ×1,15», «Экран в рамке, лицо сбоку», «Приближение экрана ×2,5». */
export function shotTitle(seg: Pick<VisualSegment, 'layout' | 'params'>): string {
  const { zoom, punch, slot, region, enter } = seg.params
  const whip = enter === 'whip' ? ', со шторкой' : ''
  if (seg.layout === 'side') return `${region ? 'Окно крупно, лицо сбоку' : 'Экран в рамке, лицо сбоку'}${whip}`
  if (seg.layout === 'screen') {
    if (!zoom) return 'Экран без лица'
    return `Приближение экрана ×${n(Math.min(1 / zoom.w, 1 / zoom.h), 1)}`
  }
  const closer = punch && punch.scale > 1 ? `, крупнее ×${n(punch.scale)}` : ''
  if (seg.layout === 'camera') return `Лицо целиком${closer}${whip}`
  return `Экран с лицом в углу ${SLOT[slot ?? 'br']}${closer}${whip}`
}

/** «A1 00:10.0–00:14.0 · 4.0 с» */
export function shotPlace(seg: Pick<VisualSegment, 'asset_id' | 'src_start_us' | 'src_end_us'>, assets: Asset[]): string {
  return `${assetRef(assets, seg.asset_id)} ${fmtUs(seg.src_start_us, 1)}–${fmtUs(seg.src_end_us, 1)} · ` +
    `${((seg.src_end_us - seg.src_start_us) / 1e6).toFixed(1)} с`
}

/** Раскладка проекта по умолчанию словами. */
export function defaultLayoutText(d: { layout: VisualSegment['layout'] } & VisualSegment['params']): string {
  return shotTitle({ layout: d.layout, params: d })
}
