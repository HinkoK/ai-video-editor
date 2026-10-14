// Режим «сразу»: слева очередь правок (ждут решения, рискованные, не
// проверены, свёрнутые проверенные), справа выбранная правка. Правки всех
// применённых предложений — одна очередь; решение уходит в своё предложение.
import { useEffect, useRef, useState } from 'react'
import { ErrorText, type PlayerHandle } from '../../components/common'
import type { Tab } from '../../App'
import { api } from '../../lib/api'
import { REVIEW_LABEL } from '../../lib/review'
import {
  nearestJoint, nextAfter, opPlace, opTitle, plural, queueCounts, riskTag, type QueueItem,
} from '../../lib/reviewQueue'
import type { Rate } from '../../lib/time'
import type { LayerItem } from '../../lib/layer'
import { SHOT_REVIEW, shotPlace } from '../../lib/shots'
import type { Joint, Project, Proposal, ProposalSummary } from '../../lib/types'
import { AfterPlayer } from './AfterPlayer'
import { EditDetail, type PlayerSlot } from './EditDetail'
import { Joints } from './Joints'
import { ShotDetail } from './ShotDetail'
import { useScreenKeys } from './keys'
import { modeSentence, openSentence, withRefs, type Previews } from './shared'
import { Subbar } from './Subbar'

export function QueueView({
  project, rate, items, proposals, open, unreviewed, held, joints, revisionNumber, previews, onOpenProposal,
  onChanged, onOpenTab, shots = [],
}: {
  project: Project
  rate: Rate
  items: QueueItem[]
  /** Кадр и графика: участки раскладки и моушн-графика, которые поставил Claude */
  shots?: LayerItem[]
  /** Применённые предложения целиком: откуда правка */
  proposals: Proposal[]
  /** Открытые предложения: отдельной группой сверху */
  open: ProposalSummary[]
  /** Счёт сервера: не проверено и ждут решения */
  unreviewed: number
  held: number
  joints: Joint[] | undefined
  revisionNumber: number | undefined
  previews: Previews
  onOpenProposal: (id: string) => void
  onChanged: () => void
  onOpenTab?: (tab: Tab) => void
}) {
  const pid = project.id
  const rid = project.current_revision_id
  const [selectedId, setSelectedId] = useState<string>()
  const [doneOpen, setDoneOpen] = useState<boolean>()
  const [jointPick, setJointPick] = useState<{ opId: string; index: number }>()
  const [player, setPlayer] = useState<PlayerSlot>('before')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>()
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const [notice, setNotice] = useState<{ opId: string; text: string }>()
  const beforeRef = useRef<PlayerHandle>(null)
  const afterRef = useRef<PlayerHandle>(null)
  const afterSlot = useRef<HTMLDivElement>(null)

  const active = items.filter((i) => i.group !== 'done')
  const done = items.filter((i) => i.group === 'done')
  const counts = queueCounts(items)
  const shotsActive = shots.filter((s) => s.review === 'pending')
  const shotsDone = shots.filter((s) => s.review !== 'pending')
  const doneVisible = doneOpen ?? (active.length === 0 && shotsActive.length === 0)
  // Порядок для стрелок: правки, затем участки кадра (так же, как в списке)
  const orderIds = [
    ...active.map((i) => i.op.id), ...shotsActive.map((s) => s.id),
    ...(doneVisible ? [...done.map((i) => i.op.id), ...shotsDone.map((s) => s.id)] : []),
  ]
  const shot = shots.find((s) => s.id === selectedId)
  const selected = shot ? undefined
    : items.find((i) => i.op.id === selectedId) ?? (selectedId === undefined ? active[0] : undefined)
  const shown = shot ?? (selectedId === undefined && !selected ? shotsActive[0] : undefined)
  const op = selected?.op
  const many = proposals.length > 1

  // Стыки: свой у правки, выбранный стрелками, ближайший для показа соседей
  const js = joints ?? []
  const own = op && op.current_joint_frame !== null ? js.findIndex((j) => j.out_frame === op.current_joint_frame) : -1
  const picked = op && jointPick?.opId === op.id ? jointPick.index : undefined
  const highlight = picked ?? own
  const anchor = highlight >= 0 ? highlight : op ? nearestJoint(js, op) : -1
  const afterJoint = highlight >= 0 && highlight < js.length ? js[highlight] : undefined
  const edits = new Map<number, number[]>()
  for (const it of items) {
    const f = it.op.current_joint_frame
    if (f !== null) edits.set(f, [...(edits.get(f) ?? []), it.n].sort((a, b) => a - b))
  }

  // Выбранная строка очереди — в поле зрения; фокус переезжает за ней
  const currentId = shown?.id ?? op?.id
  useEffect(() => {
    if (!currentId) return
    const el = document.querySelector<HTMLButtonElement>(`[data-queue-item="${currentId}"]`)
    el?.scrollIntoView({ block: 'nearest' })
    if (document.activeElement instanceof HTMLElement && document.activeElement.dataset.queueItem) el?.focus()
  }, [currentId])

  const select = (id: string | undefined) => {
    setSelectedId(id)
    setError(undefined)
  }

  const act = async (item: QueueItem, action: 'keep' | 'revert' | 'apply', reason = '') => {
    if (busy) return
    setBusy(true)
    setError(undefined)
    try {
      await api.reviewOp(pid, item.proposalId, item.op.id, action, reason)
      // Решённая уходит в «Проверены» — дальше следующая в очереди
      if (item.group !== 'done') setSelectedId(nextAfter(active.map((i) => i.op.id), item.op.id))
      onChanged()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  const canKeep = (i?: QueueItem) => i?.op.review === 'pending'
  const canRevert = (i?: QueueItem) =>
    !!i && (i.op.review === 'pending' || i.op.review === 'kept') && i.op.decision === 'accepted'

  const actShot = async (s: LayerItem, action: 'keep' | 'revert') => {
    if (busy) return
    setBusy(true)
    setError(undefined)
    try {
      await (s.kind === 'shot' ? api.reviewVisual(pid, s.id, action) : api.reviewGraphic(pid, s.id, action))
      if (s.review === 'pending') setSelectedId(nextAfter(orderIds, s.id))
      onChanged()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  const mark = async () => {
    if (!selected || !op) return
    if (op.current_joint_frame === null || !rid) {
      setNotice({ opId: op.id, text: 'У этой правки нет места в текущей версии — метку не поставить' })
      return
    }
    try {
      const m = await api.addMarker(pid, { edited_frame: op.current_joint_frame, revision_id: rid })
      setNotice({ opId: op.id, text: `Метка M${m.number} у правки ${selected.n}` })
    } catch (e) {
      setError(e)
    }
  }

  useScreenKeys((key) => {
    const current = shown?.id ?? selected?.op.id
    const at = current ? orderIds.indexOf(current) : -1
    const playerHandle = (player === 'after' && afterRef.current) || beforeRef.current
    switch (key) {
      case 'up':
      case 'down': {
        const next = orderIds[key === 'down' ? at + 1 : at < 0 ? orderIds.length - 1 : at - 1]
        if (next) select(next)
        return true
      }
      case 'enter':
        if (shown) {
          if (shown.review === 'pending') void actShot(shown, 'keep')
          return shown.review === 'pending'
        }
        if (canKeep(selected)) void act(selected!, 'keep')
        return canKeep(selected)
      case 'back':
        if (shown) {
          if (shown.review !== 'reverted') void actShot(shown, 'revert')
          return shown.review !== 'reverted'
        }
        if (canRevert(selected)) void act(selected!, 'revert')
        return canRevert(selected)
      case 'left':
      case 'right': {
        if (!op || js.length === 0) return false
        const from = anchor < 0 ? 0 : anchor
        const index = highlight < 0 ? from : Math.min(js.length - 1, Math.max(0, from + (key === 'right' ? 1 : -1)))
        setJointPick({ opId: op.id, index })
        return true
      }
      case 'j':
        playerHandle?.seekBy(-1_000_000)
        return !!playerHandle
      case 'k':
        playerHandle?.toggle()
        return !!playerHandle
      case 'l':
        playerHandle?.seekBy(1_000_000)
        return !!playerHandle
      case 'm':
        void mark()
        return true
    }
  })

  const queueRow = (it: QueueItem) => {
    const risky = it.op.risk_flags.length > 0
    const tag = it.op.review === 'held' ? 'задевает защиту'
      : it.op.review === 'kept' ? 'оставлена'
      : it.op.review === 'reverted' ? 'возвращена'
      : risky ? riskTag(it.op.risk_flags.length) : ''
    return (
      <li key={it.op.id} data-review={it.op.review} data-risk={risky ? 'yes' : 'no'}>
        <button
          type="button"
          className="rv-q"
          data-queue-item={it.op.id}
          aria-current={it.op.id === op?.id ? 'true' : undefined}
          onClick={() => select(it.op.id)}
        >
          <span className="rv-q-n">{it.n}</span>
          <span className="rv-q-main">
            <span className="rv-q-title">{opTitle(it.op)}</span>
            <span className="rv-q-place">{opPlace(it.op, project.assets)}</span>
          </span>
          <span className="rv-q-tag">{tag}</span>
        </button>
      </li>
    )
  }
  const group = (key: string, title: string, list: QueueItem[]) => list.length > 0 && (
    <section className="rv-group" aria-labelledby={`rv-g-${key}`}>
      <h3 id={`rv-g-${key}`} className="rv-group-title">
        <span className="rv-ico" data-ico={key} aria-hidden="true" />
        {title}
        <span className="rv-group-count">{list.length}</span>
      </h3>
      <ol>{list.map(queueRow)}</ol>
    </section>
  )

  const shotRow = (s: LayerItem) => (
    <li key={s.id} data-review={s.review} data-kind={s.kind}>
      <button
        type="button"
        className="rv-q"
        data-queue-item={s.id}
        aria-current={s.id === shown?.id ? 'true' : undefined}
        onClick={() => select(s.id)}
      >
        <span className="rv-q-n" />
        <span className="rv-q-main">
          <span className="rv-q-title">{s.title}</span>
          <span className="rv-q-place">{shotPlace(s, project.assets)}</span>
        </span>
        <span className="rv-q-tag">{s.review === 'pending' ? '' : SHOT_REVIEW[s.review]}</span>
      </button>
    </li>
  )

  const proposalOf = (it: QueueItem) => proposals.find((p) => p.id === it.proposalId)
  const allReviewed = (items.length > 0 || shots.length > 0) && active.length === 0 && shotsActive.length === 0

  return (
    <>
      <Subbar keys notice={notice && notice.opId === op?.id ? notice.text : undefined}>
        <span className="rv-mode">{modeSentence(project.agent_mode)}</span>
        {allReviewed ? (
          <span className="mark rv-all-ok" data-state="ok">
            {done.length === 1 ? 'Правка проверена.' : `Все ${done.length} ${plural(done.length, ['правка', 'правки', 'правок'])} проверены.`}
          </span>
        ) : (
          <>
            <span className="rv-count">Не проверено правок: <b>{unreviewed}</b>{held > 0 ? ',' : '.'}</span>
            {held > 0 && <span className="mark rv-held" data-review="held">ждут решения: <b>{held}</b></span>}
          </>
        )}
        {open.length > 0 && <span className="rv-count">{openSentence(open.map((p) => p.number))}</span>}
      </Subbar>
      <div className="rv-body" data-layout="queue">
        <aside className="rv-queue" aria-label="Очередь правок">
          <div className="rv-queue-scroll">
            {open.length > 0 && (
              <section className="rv-group" aria-labelledby="rv-g-open">
                <h3 id="rv-g-open" className="rv-group-title">
                  <span className="rv-ico" data-ico="held" aria-hidden="true" />
                  Предложения на утверждение
                  <span className="rv-group-count">{open.length}</span>
                </h3>
                <ol>
                  {open.map((p) => (
                    <li key={p.id} data-proposal="open">
                      <button type="button" className="rv-q" onClick={() => onOpenProposal(p.id)}>
                        <span className="rv-q-n" />
                        <span className="rv-q-main">
                          <span className="rv-q-title">Предложение {p.number} от {p.created_by === 'agent' ? 'Claude' : 'автора'}</span>
                          <span className="rv-q-place">правок: {p.ops_count}</span>
                        </span>
                        <span className="rv-q-tag">открыть</span>
                      </button>
                    </li>
                  ))}
                </ol>
              </section>
            )}
            {group('held', 'Ждут решения', active.filter((i) => i.group === 'held'))}
            {group('risky', 'Рискованные', active.filter((i) => i.group === 'risky'))}
            {group('pending', 'Не проверены', active.filter((i) => i.group === 'pending'))}
            {shotsActive.length > 0 && (
              <section className="rv-group" aria-labelledby="rv-g-shots">
                <h3 id="rv-g-shots" className="rv-group-title">
                  <span className="rv-ico" data-ico="pending" aria-hidden="true" />
                  Кадр и графика
                  <span className="rv-group-count">{shotsActive.length}</span>
                </h3>
                <ol>{shotsActive.map(shotRow)}</ol>
              </section>
            )}
            {doneVisible && (done.length > 0 || shotsDone.length > 0) && (
              <section className="rv-group" aria-labelledby="rv-g-done">
                <h3 id="rv-g-done" className="rv-group-title">
                  <button type="button" className="rv-group-toggle" aria-expanded="true" onClick={() => setDoneOpen(false)}>
                    <span className="rv-chevron" aria-hidden="true" />
                    Проверены
                    <span className="rv-group-count">{done.length + shotsDone.length}</span>
                  </button>
                </h3>
                <ol>{done.map(queueRow)}{shotsDone.map(shotRow)}</ol>
              </section>
            )}
          </div>
          {!doneVisible && (done.length > 0 || shotsDone.length > 0) && (
            <button type="button" className="rv-done-toggle" aria-expanded="false" onClick={() => setDoneOpen(true)}>
              <span className="rv-chevron" aria-hidden="true" />
              Проверены<span className="rv-group-count">{done.length + shotsDone.length}</span>
              <span className="rv-done-sum">оставлено {counts.kept} · возвращено {counts.reverted}</span>
            </button>
          )}
        </aside>

        {shown ? (
          <ShotDetail
            key={shown.id}
            project={project}
            seg={shown}
            rate={rate}
            error={error}
            beforeHandle={beforeRef}
            afterHandle={afterRef}
            onPlayer={setPlayer}
            actions={(
              <>
                {shown.review !== 'reverted' && (
                  <button type="button" className="btn" disabled={busy} onClick={() => actShot(shown, 'revert')}>
                    Вернуть как было<kbd aria-hidden="true">⌫</kbd>
                  </button>
                )}
                {shown.review === 'pending' && (
                  <button type="button" className="btn btn-primary" disabled={busy} onClick={() => actShot(shown, 'keep')}>
                    Оставить<kbd aria-hidden="true">↵</kbd>
                  </button>
                )}
              </>
            )}
          />
        ) : selected && op ? (
          <EditDetail
            key={op.id}
            project={project}
            op={op}
            rate={rate}
            beforeRevisionId={proposalOf(selected)?.base_revision_id}
            n={selected.n}
            attrs={{ 'data-review': op.review, 'data-risk': op.risk_flags.length > 0 ? 'yes' : 'no' }}
            status={<span className="chip" data-review={op.review}>{REVIEW_LABEL[op.review]}</span>}
            origin={many && (
              <div className="rv-origin">
                <p>Из предложения {selected.proposalNumber}: {proposalOf(selected)?.rationale}</p>
                {proposalOf(selected)?.notes.map((note) => <p key={note}>{withRefs(note, project)}</p>)}
              </div>
            )}
            actions={(canRevert(selected) || canKeep(selected)) && (
              <>
                {canRevert(selected) && (
                  <button type="button" className="btn" disabled={busy} onClick={() => act(selected, 'revert')}>
                    Вернуть как было<kbd aria-hidden="true">⌫</kbd>
                  </button>
                )}
                {canKeep(selected) && (
                  <button type="button" className="btn btn-primary" disabled={busy} onClick={() => act(selected, 'keep')}>
                    Оставить<kbd aria-hidden="true">↵</kbd>
                  </button>
                )}
              </>
            )}
            decision={(
              <>
                {op.review === 'held' && (
                  <form
                    className="rv-apply"
                    onSubmit={(e) => {
                      e.preventDefault()
                      if ((reasons[op.id] ?? '').trim()) void act(selected, 'apply', reasons[op.id])
                    }}
                  >
                    <label className="field">
                      <span className="field-label">Почему можно применить</span>
                      <input
                        type="text"
                        value={reasons[op.id] ?? ''}
                        placeholder="Например: защиту ставил на соседнюю фразу"
                        onChange={(e) => setReasons((r) => ({ ...r, [op.id]: e.target.value }))}
                      />
                    </label>
                    <button type="submit" className="btn" disabled={busy || !(reasons[op.id] ?? '').trim()}>Применить</button>
                  </form>
                )}
                <ErrorText error={error} />
              </>
            )}
            beforeHandle={beforeRef}
            onPlayer={setPlayer}
            after={(
              <div ref={afterSlot}>
                <AfterPlayer
                  key={`${rid}:${afterJoint?.out_frame ?? 'none'}`}
                  project={project}
                  op={op}
                  revisionId={rid}
                  jointFrame={afterJoint?.out_frame ?? null}
                  rate={rate}
                  previews={previews}
                  meta={`точный пробный ролик · стык ${highlight + 1}`}
                  emptyNote={
                    op.review === 'reverted' ? 'Правка возвращена: в текущей версии на этом месте нет стыка.'
                      : op.review === 'held' ? 'Правка не применена: стыка в текущей версии ещё нет.'
                      : 'У этой правки нет стыка в текущей версии.'
                  }
                  handle={afterRef}
                />
              </div>
            )}
          >
            {rid && joints && (
              <Joints
                project={project}
                revisionId={rid}
                revisionNumber={revisionNumber}
                joints={js}
                selected={highlight}
                anchor={anchor}
                own={own}
                edits={edits}
                rate={rate}
                onAfter={(index) => {
                  setJointPick({ opId: op.id, index })
                  const j = js[index]
                  if (j && rid && !previews.jobFor(rid, j.out_frame)) previews.start(rid, j.out_frame).catch(setError)
                  afterSlot.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
                }}
                onChanged={onChanged}
              />
            )}
          </EditDetail>
        ) : (
          <section className="rv-summary" aria-labelledby="rv-summary-title">
            <h2 id="rv-summary-title">{allReviewed ? 'Все правки проверены' : 'Выберите правку слева'}</h2>
            {allReviewed && (
              <>
                <p>
                  Оставлено {counts.kept}, возвращено {counts.reverted}. Ждущих решения нет. Стыки можно поправить по
                  кадрам: выберите правку слева.
                </p>
                <dl>
                  <dt>оставлено</dt><dd data-review="kept">{counts.kept}</dd>
                  <dt>возвращено</dt><dd>{counts.reverted}</dd>
                  {joints && (
                    <>
                      <dt>стыков в версии{revisionNumber !== undefined && ` ${revisionNumber}`}</dt><dd>{joints.length}</dd>
                    </>
                  )}
                </dl>
                <div className="rv-summary-actions">
                  <button type="button" className="btn" onClick={() => onOpenTab?.('export')}>Перейти к экспорту</button>
                </div>
              </>
            )}
          </section>
        )}
      </div>
    </>
  )
}
