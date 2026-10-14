import { describe, expect, it } from 'vitest'
import { groupHits, hasProxy, highlight, segmentIndexAt, segmentStart, timingText } from './transcript'
import type { Asset, Job, Segment } from './types'

function seg(id: string, start: number, end: number, asset = 'a1', speech: number | null = null): Segment {
  return {
    id, idx: 0, asset_id: asset, src_start_us: start, src_end_us: end, speech_start_us: speech, speech_end_us: null,
    text: id, text_raw: id, text_corrected: null, avg_p: null, flags: [],
  }
}

function asset(id: string, position: number): Asset {
  return {
    id, position, path: `/x/${id}.mp4`, fps: '25', frame_count: 1, duration_us: 1, width: 1, height: 1,
    role: 'main', sync_to: null, sync_offset_us: null, sync_drift_ppm: null, sync_state: null, sync: null,
  }
}

function job(type: Job['type'], asset_id: string, state: Job['state']): Job {
  return {
    id: `${type}-${asset_id}-${state}`, type, asset_id, revision_id: null, state, attempts: 1, progress: 1, message: '',
    error: null, output_path: null, created_by: 'agent', created_at: '', params: {},
  } as Job
}

describe('фразы по времени', () => {
  const list = [seg('a', 1_000_000, 2_000_000), seg('b', 3_000_000, 4_000_000), seg('c', 4_000_000, 5_000_000)]

  it('время фразы — начало речи, если известно', () => {
    expect(segmentStart(seg('x', 1_000, 9_000, 'a1', 1_500))).toBe(1_500)
    expect(segmentStart(seg('x', 1_000, 9_000))).toBe(1_000)
  })

  it('текущая фраза: звучащая, в паузе — последняя прозвучавшая', () => {
    expect(segmentIndexAt(list, 0)).toBe(-1)
    expect(segmentIndexAt(list, 1_000_000)).toBe(0)
    expect(segmentIndexAt(list, 2_500_000)).toBe(0)
    // Конец фразы не входит в неё: [начало, конец)
    expect(segmentIndexAt(list, 4_000_000)).toBe(2)
    expect(segmentIndexAt(list, 99_000_000)).toBe(2)
    expect(segmentIndexAt([], 5)).toBe(-1)
  })
})

describe('строка над списком', () => {
  it('точность времени словами', () => {
    expect(timingText({ word_timing: 'estimated', timing_level: 'segment' })).toContain('приблизительно (DTW)')
    expect(timingText({ word_timing: null, timing_level: 'segment' })).toBe('по фразам, приблизительно: резать по словам нельзя')
    expect(timingText({ word_timing: null, timing_level: 'word_verified' })).toBe('по словам, проверенная')
  })
})

describe('лёгкая копия', () => {
  it('есть, только если задача этого файла завершена', () => {
    expect(hasProxy([job('proxy', 'a1', 'completed')], 'a1')).toBe(true)
    expect(hasProxy([job('proxy', 'a1', 'running')], 'a1')).toBe(false)
    expect(hasProxy([job('proxy', 'a2', 'completed')], 'a1')).toBe(false)
    expect(hasProxy([job('transcribe', 'a1', 'completed')], 'a1')).toBe(false)
    expect(hasProxy([], 'a1')).toBe(false)
  })
})

describe('найденное', () => {
  const hits = (parts: ReturnType<typeof highlight>) => parts.filter((p) => p.hit).map((p) => p.text)

  it('без учёта регистра и по началу слова', () => {
    const parts = highlight('Стоимость одного часа примерно двадцать долларов.', 'ДОЛЛАР')
    expect(hits(parts)).toEqual(['доллар'])
    expect(parts.map((p) => p.text).join('')).toBe('Стоимость одного часа примерно двадцать долларов.')
    // «час» внутри «участок» — не начало слова, сервер его не нашёл бы
    expect(hits(highlight('участок часа', 'час'))).toEqual(['час'])
  })

  it('несколько слов, ё как е, пустой запрос', () => {
    expect(hits(highlight('Жёсткую защиту ставлю я', 'жест защ'))).toEqual(['Жёст', 'защ'])
    expect(highlight('текст', '  ')).toEqual([{ text: 'текст', hit: false }])
    expect(highlight('', 'а')).toEqual([{ text: '', hit: false }])
  })

  it('по файлам в порядке проекта, фразы по времени', () => {
    const assets = [asset('cam', 0), asset('scr', 1)]
    const groups = groupHits([seg('s2', 9, 10, 'scr'), seg('c1', 5, 6, 'cam'), seg('s1', 1, 2, 'scr')], assets)
    expect(groups.map((g) => [g.asset.id, g.number, g.hits.map((h) => h.id)])).toEqual([
      ['cam', 1, ['c1']],
      ['scr', 2, ['s1', 's2']],
    ])
    expect(groupHits([], assets)).toEqual([])
  })
})
