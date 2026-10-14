// Выбранный участок кадра или графика: что Claude показал на этом месте и
// зачем. «До» и «После» — кадр как в ролике (FrameMonitor):
// без этого участка или графики и с ними, сразу, без сборки; точный
// пробный ролик — кнопкой в «После». Решение: оставить или вернуть как было.
import { useState, type ReactNode, type RefObject } from 'react'
import { errorMessage, ErrorText, VideoPlayer, type PlayerHandle } from '../../components/common'
import { api } from '../../lib/api'
import { useJob } from '../../lib/hooks'
import { assetRef } from '../../lib/review'
import type { LayerItem } from '../../lib/layer'
import { SHOT_REVIEW, shotPlace } from '../../lib/shots'
import { fmtUs, frameToUs, usToFrame, type Rate } from '../../lib/time'
import type { Project, Revision } from '../../lib/types'
import type { PlayerSlot } from './EditDetail'
import { around, framesOf, type FrameWindow } from '../../lib/frames'
import { FrameMonitor } from './FrameMonitor'

const AROUND_US = 1_000_000
const MAX_AFTER_S = 30

export function ShotDetail({ project, seg, rate, actions, error, beforeHandle, afterHandle, onPlayer }: {
  project: Project
  seg: LayerItem
  rate: Rate
  actions?: ReactNode
  error?: unknown
  beforeHandle: RefObject<PlayerHandle | null>
  afterHandle: RefObject<PlayerHandle | null>
  onPlayer: (slot: PlayerSlot) => void
}) {
  const ref = assetRef(project.assets, seg.asset_id)
  const asset = project.assets.find((a) => a.id === seg.asset_id)
  const from = Math.max(0, seg.src_start_us - AROUND_US)
  const to = Math.min(asset?.duration_us ?? Infinity, seg.src_end_us + AROUND_US)
  const handle = (slot: PlayerSlot) => ({ onPointerDownCapture: () => onPlayer(slot), onFocusCapture: () => onPlayer(slot) })
  const params = seg.details

  return (
    <article className="rv-detail rv-shot" aria-labelledby="rv-shot-title" data-review={seg.review} data-kind={seg.kind}>
      <header className="rv-head">
        <div className="rv-head-main">
          <div className="rv-title-row">
            <h2 id="rv-shot-title" className="rv-title">{seg.kind === 'shot' ? 'Кадр' : 'Графика'}: {seg.title}</h2>
            <span className="chip" data-review={seg.review === 'reverted' ? 'reverted' : seg.review}>{SHOT_REVIEW[seg.review]}</span>
          </div>
          <p className="rv-place">
            <span className="mono">{shotPlace(seg, project.assets)}</span>
            {seg.created_by === 'agent' && <span>поставил Claude</span>}
            {!seg.in_revision && <span>в текущей версии этого места нет — вырезано</span>}
          </p>
        </div>
        {actions && <div className="rv-actions" role="group" aria-label="Решение">{actions}</div>}
      </header>
      <ErrorText error={error} />

      <div className="rv-facts">
        <section className="rv-block" aria-labelledby="rv-goal-title">
          <h3 id="rv-goal-title">Зачем</h3>
          <p className="rv-why">{seg.goal}</p>
        </section>
        <section className="rv-block" aria-labelledby="rv-params-title">
          <h3 id="rv-params-title">{seg.kind === 'shot' ? 'Как' : 'Что на экране'}</h3>
          {params.length ? <ul className="rv-shot-params">{params.map((p) => <li key={p}>{p}</li>)}</ul>
            : <p className="faint">без настроек</p>}
        </section>
      </div>

      <div className="rv-players">
        <div className="rv-player-slot" {...handle('before')}>
          {seg.in_revision ? (
            <FrameMonitor
              key={seg.id}
              project={project}
              revisionId={project.current_revision_id}
              rate={rate}
              place={(rev) => segWindow(rev, rate, seg)}
              change={{ kind: seg.kind, id: seg.id, on: false }}
              sourceAsset={seg.asset_id}
              title="До"
              meta={seg.kind === 'shot' ? 'без этого участка кадра' : 'без этой графики'}
              label={`До: кадр как в ролике без ${seg.kind === 'shot' ? 'этого участка кадра' : 'этой графики'}`}
              emptyNote="В текущей версии этого места нет — вырезано."
              handle={beforeHandle}
            />
          ) : (
            <VideoPlayer
              key={seg.id}
              src={api.proxyUrl(project.id, seg.asset_id)}
              title="До (запись экрана)"
              meta={`${ref} ${fmtUs(from, 1)}–${fmtUs(to, 1)}`}
              label={`До: ${ref}, участок кадра с запасом по секунде, лёгкая копия`}
              fromUs={from}
              toUs={to}
              cut={[seg.src_start_us, seg.src_end_us]}
              handle={beforeHandle}
            />
          )}
        </div>
        <div className="rv-player-slot" {...handle('after')}>
          <ShotAfter project={project} seg={seg} rate={rate} handle={afterHandle} />
        </div>
      </div>
    </article>
  )
}

/** Окно участка в версии: участок пунктиром, секунда запаса по краям. */
function segWindow(rev: Revision, rate: Rate, seg: LayerItem): FrameWindow | undefined {
  const r = framesOf(rev, rate, seg.asset_id, seg.src_start_us, seg.src_end_us)
  return r ? around(rev, rate, r[0], r[1], { mark: r }) : undefined
}

/** «После»: кадр как в ролике с участком или графикой; точный пробный ролик
 *  с раскладкой — по кнопке в заголовке, готовый — вместо кадра. */
function ShotAfter({ project, seg, rate, handle }: {
  project: Project
  seg: LayerItem
  rate: Rate
  handle: RefObject<PlayerHandle | null>
}) {
  const rid = project.current_revision_id
  const [jobId, setJobId] = useState<string>()
  const [error, setError] = useState<unknown>()
  const job = useJob(project.id, jobId)
  const range = seg.edited_ranges[0]
  const second = usToFrame(1_000_000, rate)
  const a = range ? Math.max(0, range[0] - second) : 0
  const b = range ? Math.min(range[1] + second, a + usToFrame(MAX_AFTER_S * 1_000_000, rate)) : 0
  const start = async () => {
    if (!rid || !range) return
    setError(undefined)
    try {
      setJobId((await api.preview(project.id, rid, a, b)).id)
    } catch (e) {
      setError(e)
    }
  }
  if (job?.state === 'completed') {
    return (
      <VideoPlayer
        src={api.jobOutputUrl(project.id, job.id)}
        title="После"
        meta="точный пробный ролик с раскладкой"
        label="После: участок в версии, с раскладкой кадра"
        offsetUs={frameToUs(a, rate)}
        handle={handle}
      />
    )
  }
  const running = job && (job.state === 'queued' || job.state === 'running')
  const failed = job?.state === 'failed'
  const extra = !range || !rid ? null : running ? (
    <span role="status">точный ролик {Math.round((job.progress ?? 0) * 100)}%</span>
  ) : (
    <>
      {failed && <span role="alert" title={job.error ?? job.message ?? ''}>точный ролик не собрался</span>}
      <button type="button" className="btn btn-quiet btn-sm" onClick={start}
        title={`Сервер соберёт участок точно, из оригиналов${b - a < (range[1] - range[0]) + 2 * second ? `, первые ${MAX_AFTER_S} с` : ''}`}>
        {failed ? 'повторить' : 'точный ролик'}
      </button>
    </>
  )
  return (
    <FrameMonitor
      key={seg.id}
      project={project}
      revisionId={rid}
      rate={rate}
      place={(rev) => segWindow(rev, rate, seg)}
      change={{ kind: seg.kind, id: seg.id, on: true }}
      sourceAsset={seg.asset_id}
      title="После"
      meta={error !== undefined ? errorMessage(error) : seg.kind === 'shot' ? 'с участком кадра' : 'с графикой'}
      extra={extra}
      label={`После: кадр как в ролике с ${seg.kind === 'shot' ? 'участком кадра' : 'графикой'}`}
      emptyNote="Участок целиком вырезан в текущей версии — показывать нечего."
      handle={handle}
    />
  )
}
