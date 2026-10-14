// Подписи фоновых задач для экранов «Задачи», «Проект» и «Экспорт»: как
// задача называется, для чего она и можно ли открыть её результат.
import { fmtUs, frameToUs, type Rate } from './time'
import type { Asset, Job } from './types'
import { baseName } from './units'

export function isActiveJob(job: Pick<Job, 'state'> | undefined): boolean {
  return !!job && (job.state === 'queued' || job.state === 'running')
}

export function jobTitle(job: Pick<Job, 'type' | 'revision_number'>): string {
  switch (job.type) {
    case 'proxy':
      return 'Лёгкая копия для просмотра'
    case 'preview':
      return 'Пробный ролик'
    case 'export':
      return job.revision_number === undefined ? 'Экспорт' : `Экспорт версии ${job.revision_number}`
    case 'analyze':
      return 'Громкость и паузы'
    case 'transcribe':
      return 'Громкость, паузы и расшифровка'
    case 'sync':
      return 'Синхронизация камеры'
    default:
      return String(job.type)
  }
}

/** Для чего задача: имя файла или версия (у пробного ролика — и кусок по времени монтажа). */
export function jobPurpose(
  job: Pick<Job, 'type' | 'asset_id' | 'revision_number' | 'params'>,
  assets: Asset[],
  rate?: Rate,
): string {
  if (job.asset_id) {
    const asset = assets.find((a) => a.id === job.asset_id)
    return asset ? baseName(asset.path) : job.asset_id
  }
  if (job.revision_number === undefined) return ''
  const version = `версия ${job.revision_number}`
  const { out_start_frame: a, out_end_frame: b } = job.params ?? {}
  if (job.type === 'preview' && rate && typeof a === 'number' && typeof b === 'number') {
    return `${version}, ${fmtUs(frameToUs(a, rate), 1)}–${fmtUs(frameToUs(b, rate), 1)}`
  }
  return version
}

/** Готовый ролик, который можно открыть в браузере (у расшифровки и
 *  громкости результат — данные, а не видео). */
export function hasOpenableOutput(job: Pick<Job, 'type' | 'state' | 'output_path'>): boolean {
  return job.state === 'completed' && !!job.output_path &&
    (job.type === 'proxy' || job.type === 'preview' || job.type === 'export')
}
