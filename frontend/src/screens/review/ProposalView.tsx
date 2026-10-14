// Режим «предлагает»: открытое предложение Claude. Слева список правок с
// флажками и фильтрами, справа выбранная правка («После» — из версии-
// кандидата), внизу утверждение. «Утвердить» заблокирована, пока список
// «чего не хватает» не пуст: это правило, сервер проверяет то же.
import { useMemo, useRef, useState } from 'react'
import { ErrorText, Select, type PlayerHandle } from '../../components/common'
import { api } from '../../lib/api'
import { useAsync } from '../../lib/hooks'
import {
  buildApproval, filterOps, initialDecisions, REASON_LABEL, RISK_LABEL, sortForReview, type Decision, type RiskFilter,
} from '../../lib/review'
import { opPlace, opTitle } from '../../lib/reviewQueue'
import type { Rate } from '../../lib/time'
import type { Project, Proposal, ProposalSummary } from '../../lib/types'
import { AfterPlayer } from './AfterPlayer'
import { EditDetail, type PlayerSlot } from './EditDetail'
import { useScreenKeys } from './keys'
import { Skeleton } from './Skeleton'
import { modeSentence, openSentence, withRefs, type Previews } from './shared'
import { Subbar } from './Subbar'

export function ProposalView({ project, proposalId, rate, open, unreviewed, held, previews, onSwitch, onBack, onDecided }: {
  project: Project
  proposalId: string
  rate: Rate
  open: ProposalSummary[]
  /** Счёт применённых правок: что ещё ждёт в очереди проверки */
  unreviewed: number
  held: number
  previews: Previews
  onSwitch: (id: string) => void
  /** К очереди проверки — если в ней что-то есть */
  onBack?: () => void
  onDecided: () => void
}) {
  const pid = project.id
  const proposal = useAsync(() => api.proposal(pid, proposalId), [pid, proposalId, project.current_revision_id])
  // Решения автора поверх начальных «принять всё»; ProposalView пересоздаётся
  // для каждого предложения (key), поэтому сбрасывать состояние не нужно
  const [changes, setDecisions] = useState<Record<string, Decision>>({})
  const [reason, setReason] = useState('all')
  const [risk, setRisk] = useState<RiskFilter>('all')
  const [selectedId, setSelectedId] = useState<string>()
  const [player, setPlayer] = useState<PlayerSlot>('before')
  const [error, setError] = useState<unknown>()
  const [busy, setBusy] = useState(false)
  const beforeRef = useRef<PlayerHandle>(null)
  const afterRef = useRef<PlayerHandle>(null)

  const ops = proposal.data?.ops
  const decisions = useMemo(() => ({ ...(ops ? initialDecisions(ops) : {}), ...changes }), [ops, changes])
  const approval = useMemo(() => (ops ? buildApproval(ops, decisions) : undefined), [ops, decisions])
  const visible = ops ? sortForReview(filterOps(ops, reason, risk)) : []
  const op = visible.find((o) => o.id === selectedId) ?? visible[0]

  useScreenKeys((key) => {
    const at = op ? visible.findIndex((o) => o.id === op.id) : -1
    const h = (player === 'after' && afterRef.current) || beforeRef.current
    switch (key) {
      case 'up':
      case 'down': {
        const next = visible[key === 'down' ? at + 1 : at - 1]
        if (next) {
          setSelectedId(next.id)
          document.querySelector(`[data-op-item="${next.id}"]`)?.scrollIntoView({ block: 'nearest' })
        }
        return true
      }
      case 'j':
        h?.seekBy(-1_000_000)
        return !!h
      case 'k':
        h?.toggle()
        return !!h
      case 'l':
        h?.seekBy(1_000_000)
        return !!h
      default:
        return false
    }
  })

  const subbar = (
    <Subbar>
      <span className="rv-mode">{modeSentence(project.agent_mode)}</span>
      <span className="rv-count">{openSentence(open.map((p) => p.number))}</span>
      {(unreviewed > 0 || held > 0) && (
        <span className="rv-count rv-also">
          В очереди проверки: не проверено {unreviewed}{held > 0 && `, ждут решения ${held}`}.
        </span>
      )}
    </Subbar>
  )

  if (proposal.error) {
    return (
      <>
        {subbar}
        <div className="rv-pad"><ErrorText error={proposal.error} /></div>
      </>
    )
  }
  if (!proposal.data || !ops || !approval) {
    return (
      <>
        {subbar}
        <Skeleton wide />
      </>
    )
  }
  const prop: Proposal = proposal.data
  const reasons = Array.from(new Set(ops.map((o) => o.reason)))
  const riskyCount = ops.filter((o) => o.risk_flags.length).length
  const excluded = approval.body.exclude_op_ids.length
  const blocked = approval.missing.length > 0

  const decide = async (approve: boolean) => {
    setBusy(true)
    setError(undefined)
    try {
      if (approve) await api.approve(pid, prop.id, approval.body)
      else await api.reject(pid, prop.id)
      onDecided()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  const change = (id: string, d: Decision) => setDecisions((all) => ({ ...all, [id]: d }))

  return (
    <>
      {subbar}
      <div className="rv-body" data-layout="propose">
        <section className="rv-prop" aria-labelledby="rv-prop-title">
          <header className="rv-prop-head">
            {(onBack || open.length > 1) && (
              <nav className="rv-prop-nav" aria-label="Предложения и очередь">
                {onBack && <button type="button" className="btn btn-quiet" onClick={onBack}>← Очередь проверки</button>}
                {open.length > 1 && open.map((p) => (
                  <button key={p.id} type="button" className="btn btn-quiet" aria-pressed={p.id === prop.id} onClick={() => onSwitch(p.id)}>
                    Предложение {p.number}
                  </button>
                ))}
              </nav>
            )}
            <div className="rv-prop-title-row">
              <h2 id="rv-prop-title" className="rv-prop-title">
                Предложение {prop.number} от {prop.created_by === 'agent' ? 'Claude' : 'автора'}
              </h2>
              <p className="rv-prop-counts">
                Правок: <b>{ops.length}</b>, рискованных: <b className={riskyCount > 0 ? 'rv-warn' : undefined}>{riskyCount}</b>
              </p>
            </div>
            {(prop.rationale || prop.notes.length > 0) && (
              <div className="rv-origin">
                {prop.rationale && <p>{prop.rationale}</p>}
                {prop.notes.map((n) => <p key={n}>{withRefs(n, project)}</p>)}
              </div>
            )}
            <div className="rv-filters">
              <label className="inline-label">
                Причина
                <Select small className="rv-filter-reason" value={reason} onChange={(e) => setReason(e.target.value)}>
                  <option value="all">все</option>
                  {reasons.map((r) => <option key={r} value={r}>{REASON_LABEL[r] ?? r}</option>)}
                </Select>
              </label>
              <label className="inline-label">
                Риск
                <Select small className="rv-filter-risk" value={risk} onChange={(e) => setRisk(e.target.value as RiskFilter)}>
                  <option value="all">все</option>
                  <option value="risky">только рискованные</option>
                  <option value="clean">без риска</option>
                </Select>
              </label>
            </div>
          </header>
          <ol className="rv-ops">
            {visible.length === 0 && <li className="rv-op faint" data-state="empty">Под фильтр ничего не попало.</li>}
            {visible.map((o) => {
              const d = decisions[o.id] ?? { include: true, acknowledged: false, override: '' }
              const risky = o.risk_flags.length > 0
              return (
                <li
                  key={o.id}
                  className="rv-op"
                  data-risk={risky ? 'yes' : 'no'}
                  data-included={d.include ? 'yes' : 'no'}
                  aria-current={o.id === op?.id ? 'true' : undefined}
                >
                  <button type="button" className="rv-op-select" data-op-item={o.id} onClick={() => setSelectedId(o.id)}>
                    <span className="rv-q-n">{o.position + 1}</span>
                    <span className="rv-op-title">{opTitle(o)}</span>
                    <span className="rv-q-place">{opPlace(o, project.assets)}</span>
                  </button>
                  {risky && (
                    <ul className="rv-op-risks" aria-label="Риски">
                      {o.risk_flags.map((f) => <li key={f} className="mark" data-risk={f}>{RISK_LABEL[f]}</li>)}
                    </ul>
                  )}
                  <div className="rv-op-checks">
                    <label className="check">
                      <input type="checkbox" checked={d.include} onChange={(e) => change(o.id, { ...d, include: e.target.checked })} />
                      принять эту правку
                    </label>
                    {risky && (
                      <label className="check">
                        <input
                          type="checkbox"
                          checked={d.acknowledged}
                          disabled={!d.include}
                          onChange={(e) => change(o.id, { ...d, acknowledged: e.target.checked })}
                        />
                        проверил отдельно
                      </label>
                    )}
                  </div>
                  {d.include && o.risk_flags.includes('protected') && (
                    <label className="field rv-op-reason">
                      <span className="field-label">Почему можно вырезать защищённое</span>
                      <input
                        type="text"
                        value={d.override}
                        placeholder="Например: оговорка, верное число звучит через 10 с"
                        onChange={(e) => change(o.id, { ...d, override: e.target.value })}
                      />
                    </label>
                  )}
                </li>
              )
            })}
          </ol>
        </section>

        {op ? (
          <EditDetail
            key={op.id}
            project={project}
            op={op}
            rate={rate}
            beforeRevisionId={prop.base_revision_id}
            n={op.position + 1}
            attrs={{
              'data-risk': op.risk_flags.length > 0 ? 'yes' : 'no',
              'data-included': (decisions[op.id]?.include ?? true) ? 'yes' : 'no',
            }}
            narrowRisks
            showRequested
            beforeHandle={beforeRef}
            onPlayer={setPlayer}
            after={(
              <AfterPlayer
                key={`${prop.candidate_revision_id}:${op.candidate_joint_frame ?? 'none'}`}
                project={project}
                op={op}
                revisionId={prop.candidate_revision_id}
                jointFrame={op.candidate_joint_frame}
                rate={rate}
                previews={previews}
                meta="точный пробный ролик · версия-кандидат"
                emptyNote="У этой правки нет стыка в версии-кандидате."
                handle={afterRef}
              />
            )}
          />
        ) : (
          <section className="rv-summary"><h2>Выберите правку слева</h2></section>
        )}
      </div>
      <footer className="rv-footer">
        {blocked ? (
          <div id="rv-why-blocked" className="rv-footer-why">
            <p>Утвердить пока нельзя:</p>
            <ul aria-label="Чего не хватает для утверждения" data-state="blocked">
              {approval.missing.map((m) => <li key={m} className="mark rv-blocker">{m}</li>)}
            </ul>
          </div>
        ) : (
          <p className="mark" data-state="ok">Всё отмечено, можно утверждать.</p>
        )}
        <ErrorText error={error} />
        <div className="rv-footer-actions">
          <button type="button" className="btn btn-danger" disabled={busy} onClick={() => decide(false)}>Отклонить всё</button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || blocked}
            aria-describedby={blocked ? 'rv-why-blocked' : undefined}
            onClick={() => decide(true)}
          >
            Утвердить{excluded > 0 && ` (кроме ${excluded})`}
          </button>
        </div>
      </footer>
    </>
  )
}
