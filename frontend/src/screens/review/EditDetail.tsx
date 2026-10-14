// Выбранная правка: заголовок со статусом, место и сдвиг границы, «Почему»
// и риски, плееры «До» и «После», текст вокруг. Общая для режимов
// «сразу» и «предлагает»; кнопки решения и стыки передаёт экран.
import type { ReactNode, RefObject } from 'react'
import { VideoPlayer, type PlayerHandle } from '../../components/common'
import { api } from '../../lib/api'
import { assetRef, RISK_LABEL } from '../../lib/review'
import { opRange, opSeconds, opTitle } from '../../lib/reviewQueue'
import { fmtUs, type Rate } from '../../lib/time'
import type { Op, Project } from '../../lib/types'
import { Context } from './Context'
import { cutWindow } from '../../lib/frames'
import { FrameMonitor } from './FrameMonitor'
import { AROUND_US, withRefs } from './shared'

export type PlayerSlot = 'before' | 'after'

export function EditDetail({
  project, op, n, status, actions, origin, decision, after, beforeHandle, onPlayer, narrowRisks, showRequested,
  children, attrs, rate, beforeRevisionId,
}: {
  project: Project
  op: Op
  rate: Rate
  /** Версия до правки: «До» — кадр как в ролике в ней. Нет — запись экрана, как раньше */
  beforeRevisionId?: string | null
  /** Номер правки в заголовке */
  n: number
  /** Значок статуса рядом с заголовком */
  status?: ReactNode
  /** Кнопки решения справа вверху */
  actions?: ReactNode
  /** Откуда правка: предложение, пометки сервера */
  origin?: ReactNode
  /** Блок решения под заголовком (поле причины у ждущей решения) */
  decision?: ReactNode
  /** Плеер «После» */
  after: ReactNode
  beforeHandle: RefObject<PlayerHandle | null>
  /** Каким плеером управляют J K L: тем, которого касались последним */
  onPlayer: (slot: PlayerSlot) => void
  narrowRisks?: boolean
  /** Показать, какие границы просил Claude (до выравнивания сервером) */
  showRequested?: boolean
  children?: ReactNode
  attrs?: Record<string, string>
}) {
  const ref = assetRef(project.assets, op.asset_id)
  const asset = project.assets.find((a) => a.id === op.asset_id)
  const from = Math.max(0, op.src_start_us - AROUND_US)
  const to = Math.min(asset?.duration_us ?? Infinity, op.src_end_us + AROUND_US)
  const moved = showRequested && op.requested_start_us !== null &&
    (op.requested_start_us !== op.src_start_us || op.requested_end_us !== op.src_end_us)
  const handle = (slot: PlayerSlot) => ({ onPointerDownCapture: () => onPlayer(slot), onFocusCapture: () => onPlayer(slot) })
  const doing = op.kind === 'remove' ? 'вырезается' : op.kind === 'move' ? 'переставляется' : 'возвращается'

  return (
    <article className="rv-detail" aria-labelledby="rv-detail-title" {...attrs}>
      <header className="rv-head">
        <div className="rv-head-main">
          <div className="rv-title-row">
            <h2 id="rv-detail-title" className="rv-title">{n}. {opTitle(op)}</h2>
            {status}
          </div>
          <p className="rv-place">
            <span className="mono">{opRange(op, project.assets)}</span>
            {op.adjust_note && <span data-role="adjust-note">{withRefs(op.adjust_note, project)}</span>}
            {moved && (
              <span>просили {fmtUs(op.requested_start_us, 1)}–{fmtUs(op.requested_end_us, 1)}</span>
            )}
          </p>
          {origin}
        </div>
        {actions && <div className="rv-actions" role="group" aria-label="Решение">{actions}</div>}
      </header>

      {decision}

      <div className="rv-facts" data-narrow={narrowRisks ? 'yes' : undefined}>
        <section className="rv-block" aria-labelledby="rv-why-title">
          <h3 id="rv-why-title">Почему</h3>
          <p className="rv-why">{op.why || '—'}</p>
        </section>
        <section className="rv-block" aria-labelledby="rv-risk-title">
          <h3 id="rv-risk-title">Риски: {op.risk_flags.length}</h3>
          {op.risk_flags.length > 0 ? (
            <ul className="rv-risks" aria-label="Риски">
              {op.risk_flags.map((f) => <li key={f} className="mark" data-risk={f}>{RISK_LABEL[f]}</li>)}
            </ul>
          ) : (
            <p className="faint">нет пометок</p>
          )}
        </section>
      </div>

      <div className="rv-players">
        <div className="rv-player-slot" {...handle('before')}>
          {beforeRevisionId !== undefined ? (
            <FrameMonitor
              key={op.id}
              project={project}
              revisionId={beforeRevisionId}
              rate={rate}
              place={(rev) => cutWindow(rev, rate, op.asset_id, op.src_start_us, op.src_end_us, AROUND_US)}
              sourceAsset={op.asset_id}
              title="До"
              meta={`версия до правки · ${doing} ${opSeconds(op)} с`}
              label={op.kind === 'move'
                ? `До: кадр как в ролике в версии до правки, где кусок стоял, ${ref}`
                : `До: кадр как в ролике в версии до правки, ${ref} вокруг правки`}
              emptyNote="В версии до правки этого места нет."
              handle={beforeHandle}
            />
          ) : (
          <VideoPlayer
            key={op.id}
            src={api.proxyUrl(project.id, op.asset_id)}
            title="До (исходник)"
            meta={`${ref} ${fmtUs(from, 1)}–${fmtUs(to, 1)} · ${doing} ${opSeconds(op)} с`}
            label={`До: ${ref} вокруг правки, лёгкая копия, время исходника`}
            fromUs={from}
            toUs={to}
            cut={[op.src_start_us, op.src_end_us]}
            handle={beforeHandle}
          />
          )}
        </div>
        <div className="rv-player-slot" {...handle('after')}>{after}</div>
      </div>

      <Context
        projectId={project.id}
        op={op}
        onSeek={(us) => {
          onPlayer('before')
          beforeHandle.current?.seekTo(us)
        }}
      />

      {children}
    </article>
  )
}
