import { describe, expect, it } from 'vitest'
import { hasOpenableOutput, isActiveJob, jobPurpose, jobTitle } from './jobInfo'
import { parseRate } from './time'
import type { Asset, Job } from './types'

const assets: Asset[] = [
  { id: 'a1', position: 0, path: '/Users/me/Видео/камера.mp4', fps: '30000/1001', frame_count: 100, duration_us: 1, width: 1920, height: 1080,
    role: 'main', sync_to: null, sync_offset_us: null, sync_drift_ppm: null, sync_state: null, sync: null },
]

const job = (over: Partial<Job>): Job => ({
  id: 'j', type: 'proxy', asset_id: null, revision_id: null, state: 'completed', attempts: 1, progress: 1,
  message: '', error: null, output_path: null, created_by: 'user', created_at: '', params: {}, ...over,
})

describe('подписи задач', () => {
  it('название по типу', () => {
    expect(jobTitle(job({ type: 'proxy' }))).toBe('Лёгкая копия для просмотра')
    expect(jobTitle(job({ type: 'preview' }))).toBe('Пробный ролик')
    expect(jobTitle(job({ type: 'export', revision_number: 4 }))).toBe('Экспорт версии 4')
    expect(jobTitle(job({ type: 'export' }))).toBe('Экспорт')
    expect(jobTitle(job({ type: 'analyze' }))).toBe('Громкость и паузы')
    expect(jobTitle(job({ type: 'transcribe' }))).toBe('Громкость, паузы и расшифровка')
  })

  it('для чего: файл по asset_id или версия', () => {
    expect(jobPurpose(job({ asset_id: 'a1' }), assets)).toBe('камера.mp4')
    expect(jobPurpose(job({ asset_id: 'нет' }), assets)).toBe('нет')
    expect(jobPurpose(job({ type: 'export', revision_number: 4 }), assets)).toBe('версия 4')
    expect(jobPurpose(job({}), assets)).toBe('')
  })

  it('у пробного ролика — ещё и кусок по времени монтажа', () => {
    const preview = job({ type: 'preview', revision_number: 4, params: { out_start_frame: 177, out_end_frame: 357 } })
    expect(jobPurpose(preview, assets, parseRate('30000/1001'))).toBe('версия 4, 00:05.9–00:11.9')
    expect(jobPurpose(preview, assets)).toBe('версия 4')
  })

  it('открыть можно только готовый ролик с файлом', () => {
    expect(hasOpenableOutput(job({ type: 'export', output_path: '/x.mp4' }))).toBe(true)
    expect(hasOpenableOutput(job({ type: 'export', output_path: null }))).toBe(false)
    expect(hasOpenableOutput(job({ type: 'transcribe', output_path: '/dir' }))).toBe(false)
    expect(hasOpenableOutput(job({ type: 'proxy', state: 'failed', output_path: '/x.mp4' }))).toBe(false)
  })

  it('идущая задача — в очереди или идёт', () => {
    expect(isActiveJob(job({ state: 'queued' }))).toBe(true)
    expect(isActiveJob(job({ state: 'running' }))).toBe(true)
    expect(isActiveJob(job({ state: 'interrupted' }))).toBe(false)
    expect(isActiveJob(undefined)).toBe(false)
  })
})
