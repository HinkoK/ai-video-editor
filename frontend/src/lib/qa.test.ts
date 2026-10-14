import { describe, expect, it } from 'vitest'
import { checkDetail, checkPassed, checkStatus, checkTitle, checksSummary, reportFrames, reportRevisionNumber } from './qa'

// Так сервер записывает проверки экспорта (worker._check_export)
const technical = {
  frames: { ok: true, expected: 71060, actual: 71060 },
  constant_frame_rate: { ok: true },
  audio_samples: { ok: false, expected: 3281678, actual: 3281600 },
  matches_revision: { ok: true, points: 13, video_mismatches: [], audio_mismatches: [], video_ambiguous: 0, audio_checked: 12 },
  protected_ranges: { ok: true, cut_without_reason: [] },
  revision: { ok: true, id: 'rev', number: 4 },
  agent_review: { ok: true, unreviewed: 4, held: 1 },
}

describe('отчёт качества', () => {
  it('названия проверок; незнакомая — по ключу', () => {
    expect(checkTitle('frames')).toBe('Число кадров')
    expect(checkTitle('matches_revision')).toBe('Сверка с исходником')
    expect(checkTitle('новая')).toBe('новая')
  })

  it('подробности — только из данных проверки', () => {
    expect(checkDetail('frames', technical.frames)).toBe('71 060 = 71 060')
    expect(checkDetail('audio_samples', technical.audio_samples))
      .toBe('получено 3 281 600, нужно 3 281 678 отсчётов')
    expect(checkDetail('audio_samples', { ok: false, expected: 10, actual: null }))
      .toBe('длину звуковой дорожки прочитать не удалось')
    expect(checkDetail('constant_frame_rate', technical.constant_frame_rate)).toBe('')
    expect(checkDetail('matches_revision', technical.matches_revision)).toBe('мест сверки: 13 · звук проверен в 12')
    expect(checkDetail('matches_revision', { ok: false, points: 5, video_mismatches: [{}], audio_mismatches: [], video_ambiguous: 2 }))
      .toBe('мест сверки: 5 · неразличимо: 2 · расхождений в кадре: 1')
    expect(checkDetail('protected_ranges', technical.protected_ranges)).toBe('вырезов в защищённом без причины: 0')
    expect(checkDetail('revision', technical.revision)).toBe('версия 4')
    expect(checkDetail('agent_review', technical.agent_review)).toBe('не проверено: 4 · ждут решения: 1')
    expect(checkDetail('новая', { ok: true, x: 1 })).toBe('')
  })

  it('сколько пройдено; без ok — пройдено', () => {
    expect(checkPassed({})).toBe(true)
    expect(checkPassed({ ok: false })).toBe(false)
    // Правки Claude: сервер пишет ok, но непроверенное — напоминание, не «пройдено»
    expect(checkStatus('agent_review', { ok: true, unreviewed: 4, held: 1 })).toBe('remind')
    expect(checkStatus('agent_review', { ok: true, unreviewed: 0, held: 0 })).toBe('ok')
    expect(checkStatus('frames', { ok: false })).toBe('fail')
    expect(checkStatus('loudness', { ok: true, capped_by_peak: true })).toBe('remind')
    expect(checkDetail('loudness', {
      ok: true, before_lufs: -21.3, after_lufs: -16, target_lufs: -16, gain_db: 5.3, applied: true,
      after_true_peak: -3.2, capped_by_peak: false,
    })).toBe('−21,3 → −16,0 LUFS · цель −16,0 · усиление +5,3 дБ · пик −3,2 dBTP')
    expect(checksSummary({ technical: { frames: { ok: true }, agent_review: { ok: true, unreviewed: 0, held: 2 } } }))
      .toEqual({ passed: 1, total: 2, reminders: 1 })
    expect(checksSummary({ technical })).toEqual({ passed: 5, total: 7, reminders: 1 })
  })

  it('номер версии и число кадров ролика', () => {
    expect(reportRevisionNumber({ technical })).toBe(4)
    expect(reportFrames({ technical })).toBe(71060)
    expect(reportRevisionNumber({ technical: {} })).toBeUndefined()
  })
})
