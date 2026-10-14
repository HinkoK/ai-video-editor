// «Текст вокруг»: фразы расшифровки рядом с правкой; то, что уходит, помечено.
import { ErrorText } from '../../components/common'
import { api } from '../../lib/api'
import { useAsync } from '../../lib/hooks'
import { fmtUs } from '../../lib/time'
import type { Op, Segment } from '../../lib/types'

export function Context({ projectId, op, onSeek }: {
  projectId: string
  op: Op
  /** Переход «До» к началу фразы (время исходника) */
  onSeek?: (us: number) => void
}) {
  const segs = useAsync(
    () => api.transcript(projectId, op.asset_id, Math.max(0, op.src_start_us - 8_000_000), op.src_end_us + 8_000_000, 40),
    [projectId, op.id],
  )
  const removed = (s: Segment) => {
    const a = s.speech_start_us ?? s.src_start_us
    const b = s.speech_end_us ?? s.src_end_us
    return op.kind === 'remove' && Math.min(b, op.src_end_us) - Math.max(a, op.src_start_us) > (b - a) / 2
  }
  return (
    <section className="rv-block rv-around" aria-labelledby="rv-around-title">
      <h3 id="rv-around-title">Текст вокруг</h3>
      <ErrorText error={segs.error} />
      {!segs.data && !segs.error && <p className="loading-line" data-state="loading">Загружаю текст…</p>}
      {segs.data && !segs.data.transcript && <p className="faint" data-state="empty">Расшифровки этого файла ещё нет.</p>}
      {segs.data?.transcript && segs.data.segments.length === 0 && (
        <p className="faint" data-state="empty">Рядом с правкой нет фраз: здесь тишина.</p>
      )}
      {segs.data?.transcript && segs.data.segments.length > 0 && (
        <ol aria-label="Текст вокруг правки">
          {segs.data.segments.map((s) => {
            const gone = removed(s)
            const at = s.speech_start_us ?? s.src_start_us
            return (
              <li key={s.id} data-removed={gone ? 'yes' : 'no'} data-flags={s.flags.join(' ')}>
                <button type="button" className="time-link" onClick={() => onSeek?.(at)} aria-label={`До: перейти к ${fmtUs(at, 1)}`}>
                  {fmtUs(at, 1)}
                </button>
                <p>{s.text}</p>
                {gone ? <span className="chip sm" data-removed="yes">уходит</span> : <span />}
              </li>
            )
          })}
        </ol>
      )}
    </section>
  )
}
