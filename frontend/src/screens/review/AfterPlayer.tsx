// «После»: кадр как в ролике в версии после правки — сразу, без сборки
// (FrameMonitor), черта — стык. Точный пробный ролик стыка
// сервер собирает по кнопке «точный ролик» в заголовке: готовый — показывается
// вместо кадра, как раньше; пока считается — проценты и «отменить».
import { useState, type Ref } from 'react'
import { errorMessage, VideoPlayer, type PlayerHandle } from '../../components/common'
import { api } from '../../lib/api'
import { useJob } from '../../lib/hooks'
import { frameToUs, type Rate } from '../../lib/time'
import type { Op, Project, Revision } from '../../lib/types'
import { around, framesOf, type FrameWindow } from '../../lib/frames'
import { FrameMonitor } from './FrameMonitor'
import { AROUND_US, previewWindow, type Previews } from './shared'

export function AfterPlayer({ project, op, revisionId, jointFrame, rate, previews, meta, emptyNote, handle }: {
  project: Project
  /** Правка: возвращённый кусок отмечается на полосе пунктиром */
  op?: Op
  revisionId: string | null
  jointFrame: number | null
  rate: Rate
  previews: Previews
  /** Подпись готового ролика: «точный пробный ролик · стык 6» */
  meta: string
  /** Почему показывать нечего (стыка в версии нет) */
  emptyNote: string
  handle?: Ref<PlayerHandle>
}) {
  const pid = project.id
  const jobId = revisionId && jointFrame !== null ? previews.jobFor(revisionId, jointFrame) : undefined
  const job = useJob(pid, jobId)
  const [error, setError] = useState<unknown>()
  const [busy, setBusy] = useState(false)

  const build = async () => {
    if (!revisionId || jointFrame === null) return
    setBusy(true)
    setError(undefined)
    try {
      await previews.start(revisionId, jointFrame)
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  const cancel = async () => {
    if (!jobId) return
    setError(undefined)
    try {
      await api.cancelJob(pid, jobId)
    } catch (e) {
      setError(e)
    }
  }

  if (revisionId && jointFrame !== null && job?.state === 'completed') {
    const from = Number(job.params.out_start_frame ?? 0)
    const offset = frameToUs(from, rate)
    return (
      <VideoPlayer
        key={job.id}
        src={api.jobOutputUrl(pid, job.id)}
        title="После"
        meta={meta}
        label="После: точный пробный ролик стыка, время монтажа"
        offsetUs={offset}
        jointUs={frameToUs(jointFrame, rate) - offset}
        handle={handle}
      />
    )
  }

  const state = jointFrame === null ? 'none'
    : !jobId || job?.state === 'cancelled' ? 'idle'
    : !job || job.state === 'queued' || job.state === 'running' ? 'running'
    : 'error'
  const pct = Math.round((job?.progress ?? 0) * 100)
  const failure = state === 'error' ? job?.error ?? job?.message ?? '' : error !== undefined ? errorMessage(error) : ''
  const extra = state === 'idle' ? (
    <button type="button" className="btn btn-quiet btn-sm" disabled={busy} onClick={build}
      title="Сервер соберёт этот стык точно, из оригиналов, со звуком до отсчёта">точный ролик</button>
  ) : state === 'running' ? (
    <>
      <span role="status">точный ролик {pct}%</span>
      <button type="button" className="btn btn-quiet btn-sm" onClick={cancel}>отменить</button>
    </>
  ) : state === 'error' ? (
    <>
      <span role="alert" title={failure}>точный ролик не собрался</span>
      <button type="button" className="btn btn-quiet btn-sm" disabled={busy} onClick={build}>повторить</button>
    </>
  ) : null

  const place = (rev: Revision): FrameWindow | undefined => {
    if (op?.kind === 'restore') {
      const r = framesOf(rev, rate, op.asset_id, op.src_start_us, op.src_end_us)
      if (r) return around(rev, rate, r[0], r[1], { mark: r }, AROUND_US)
    }
    if (op?.kind === 'move') {
      // Переставлено: кусок на новом месте пунктиром, черта — его вход (стык)
      const r = framesOf(rev, rate, op.asset_id, op.src_start_us, op.src_end_us)
      if (r) return around(rev, rate, r[0], r[1], { mark: r, ...(jointFrame !== null ? { joint: jointFrame } : {}) }, AROUND_US)
    }
    if (jointFrame === null) return undefined
    const [a, b] = previewWindow(jointFrame, rate)
    return { from: a, to: Math.min(b, rev.total_frames), joint: jointFrame }
  }

  return (
    <FrameMonitor
      project={project}
      revisionId={revisionId}
      rate={rate}
      place={place}
      sourceAsset={op?.asset_id}
      title="После"
      meta={failure && state !== 'error' ? failure : 'кадр как в ролике'}
      extra={extra}
      label="После: кадр как в ролике в версии после правки, стык чертой"
      emptyNote={emptyNote}
      handle={handle}
    />
  )
}
