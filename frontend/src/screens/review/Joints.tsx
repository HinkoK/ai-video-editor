// «Стыки текущей версии»: соседние со стыком выбранной правки (или все),
// сдвиг границы на N кадров и пробный ролик «После» для любого стыка.
import { useState } from 'react'
import { ErrorText } from '../../components/common'
import { api } from '../../lib/api'
import { assetRef } from '../../lib/review'
import { jointWindow } from '../../lib/reviewQueue'
import { fmtUs, frameToUs, type Rate } from '../../lib/time'
import type { Joint, Project } from '../../lib/types'

export function Joints({
  project, revisionId, revisionNumber, joints, selected, anchor, own, edits, rate, onAfter, onChanged,
}: {
  project: Project
  revisionId: string
  revisionNumber: number | undefined
  joints: Joint[]
  /** Выделенный стык (← →), -1 — нет */
  selected: number
  /** Вокруг какого стыка показывать соседние */
  anchor: number
  /** Стык выбранной правки, -1 — у неё нет стыка в версии */
  own: number
  /** Номера правок у стыка, по кадру стыка */
  edits: Map<number, number[]>
  rate: Rate
  /** «После» у строки: выделить стык и показать его пробный ролик */
  onAfter: (index: number) => void
  onChanged: () => void
}) {
  const [all, setAll] = useState(false)
  const [error, setError] = useState<unknown>()
  const [a, b] = all ? [0, joints.length] : jointWindow(joints.length, Math.max(0, anchor))
  const nudge = async (joint: number, side: 'before' | 'after', frames: number) => {
    setError(undefined)
    try {
      await api.nudge(project.id, revisionId, joint, side, frames)
      onChanged()
    } catch (e) {
      setError(e)
    }
  }
  const windowed = joints.length > b - a
  return (
    <section className="rv-block rv-joints" aria-labelledby="rv-joints-title">
      <div className="rv-joints-head">
        <h3 id="rv-joints-title">Стыки текущей версии</h3>
        <span className="rv-joints-note">
          {revisionNumber !== undefined && `версия ${revisionNumber} · `}стыков {joints.length}
          {windowed && ` · показаны соседние со стыком ${Math.max(0, anchor) + 1}`}
        </span>
        {joints.length > 4 && (
          <button type="button" className="btn btn-quiet" aria-expanded={all} onClick={() => setAll((v) => !v)}>
            {all ? 'только соседние' : 'показать все'}
          </button>
        )}
      </div>
      <ErrorText error={error} />
      {joints.length === 0 ? (
        <p className="faint" data-state="empty">Стыков нет: ролик идёт без вырезов.</p>
      ) : (
        <ol>
          {joints.slice(a, b).map((j, k) => (
            // Ключ включает версию и обе границы: после сдвига стыка старый
            // пробный ролик не должен остаться под видом нового
            <JointRow
              key={`${revisionId}:${j.out_frame}:${j.before_src_end_us}:${j.after_src_start_us}`}
              project={project}
              joint={j}
              index={a + k}
              current={a + k === selected}
              own={a + k === own}
              edits={edits.get(j.out_frame) ?? []}
              rate={rate}
              onNudge={nudge}
              onAfter={onAfter}
            />
          ))}
        </ol>
      )}
    </section>
  )
}

function JointRow({ project, joint: j, index, current, own, edits, rate, onNudge, onAfter }: {
  project: Project
  joint: Joint
  index: number
  current: boolean
  own: boolean
  edits: number[]
  rate: Rate
  onNudge: (joint: number, side: 'before' | 'after', frames: number) => void
  onAfter: (index: number) => void
}) {
  const [frames, setFrames] = useState(5)
  const note = [
    edits.length === 1 ? `правка ${edits[0]}` : edits.length > 1 ? `правки ${edits.join(', ')}` : '',
    j.kind === 'move' ? 'стык перестановки' : '',
    own ? 'выбрана' : '',
  ].filter(Boolean).join(' · ')
  return (
    <li className="rv-joint" aria-current={current ? 'true' : undefined}>
      <span className="rv-joint-text">
        <span className="rv-joint-place" title={`точно: ${fmtUs(j.before_src_end_us)} → ${fmtUs(j.after_src_start_us)}`}>
          Стык {index + 1} на {fmtUs(frameToUs(j.out_frame, rate), 1)}: {assetRef(project.assets, j.before_asset)}{' '}
          {fmtUs(j.before_src_end_us, 1)} → {assetRef(project.assets, j.after_asset)} {fmtUs(j.after_src_start_us, 1)}
        </span>
        {note && <span className="rv-joint-note">{note}</span>}
      </span>
      <span className="rv-joint-controls">
        <label className="rv-joint-frames">
          Сдвиг, кадров
          <input
            type="number"
            min={1}
            max={250}
            value={frames}
            // Направление задают кнопки, здесь только величина
            onChange={(e) => setFrames(Math.min(250, Math.max(1, Math.abs(Math.round(Number(e.target.value))) || 1)))}
          />
        </label>
        <span className="rv-joint-buttons" role="group" aria-label={`Стык ${index + 1}`}>
          <button type="button" className="btn btn-quiet" onClick={() => onNudge(index, 'before', frames)}>вернуть конец</button>
          <button type="button" className="btn btn-quiet" onClick={() => onNudge(index, 'before', -frames)}>убрать с конца</button>
          <button type="button" className="btn btn-quiet" onClick={() => onNudge(index, 'after', frames)}>вернуть начало</button>
          <button type="button" className="btn btn-quiet" onClick={() => onNudge(index, 'after', -frames)}>убрать с начала</button>
          <button type="button" className="btn btn-sm" onClick={() => onAfter(index)}>После</button>
        </span>
      </span>
    </li>
  )
}
