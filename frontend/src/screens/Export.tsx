// Экспорт утверждённой версии и отчёт качества. Три статуса отчёта не
// подменяют друг друга: проверки кода, замечания модели, проверка человеком.
// Оформление — styles/export.css.
import { useEffect, useRef, useState, type FormEvent } from 'react'
import type { Tab } from '../App'
import { ErrorText, JOB_STATE, StatePanel, errorMessage } from '../components/common'
import { api } from '../lib/api'
import { useAsync, useInterval, useJob } from '../lib/hooks'
import { isActiveJob, jobTitle } from '../lib/jobInfo'
import { checkDetail, checkStatus, checkTitle, checksSummary, reportFrames, reportRevisionNumber } from '../lib/qa'
import { fmtUs, frameToUs, type Rate } from '../lib/time'
import type { Job, Project, QaReport } from '../lib/types'
import { baseName, fmtShortDate } from '../lib/units'

// Сервер отдаёт и время проверки человеком (human_at), в types.ts его нет
type Report = QaReport & { human_at?: string | null }

export function Export({ project, onOpenTab }: { project: Project; onOpenTab?: (tab: Tab) => void }) {
  const pid = project.id
  const rid = project.current_revision_id
  const rate: Rate | undefined = project.fps_num && project.fps_den ? { num: project.fps_num, den: project.fps_den } : undefined
  const reports = useAsync(() => api.qa(pid) as Promise<Report[]>, [pid])
  const jobs = useAsync(() => api.jobs(pid), [pid])
  const revisions = useAsync(() => api.revisions(pid), [pid, rid])
  const [jobId, setJobId] = useState<string>()
  const [error, setError] = useState<unknown>()
  const started = useJob(pid, jobId)

  const exports = (jobs.data ?? []).filter((j) => j.type === 'export')
  // Идущий экспорт: запущенный здесь или найденный в списке задач (запущен раньше или Claude)
  const running = isActiveJob(started) ? started : exports.find((j) => isActiveJob(j) && j.id !== started?.id)
  useInterval(jobs.reload, 2000, !!running && running !== started)
  // Экспорт закончился — показать новый отчёт и обновить список задач
  const activeId = running?.id
  const prevActive = useRef<string | undefined>(undefined)
  const { reload: reloadReports } = reports
  const { reload: reloadJobs } = jobs
  useEffect(() => {
    if (prevActive.current && !activeId) {
      reloadReports()
      reloadJobs()
    }
    prevActive.current = activeId
  }, [activeId, reloadReports, reloadJobs])

  const number = revisions.data?.find((r) => r.id === rid)?.number ?? running?.revision_number
  const lastExport = started ?? exports[0]
  const failed = !running && lastExport?.state === 'failed' ? lastExport : undefined
  const list = reports.data ?? []
  const exported = list.some((r) => r.revision_id === rid && !r.stale)
  const canStart = !!rid && !running

  const start = async () => {
    if (!rid) return
    setError(undefined)
    try {
      setJobId((await api.exportRevision(pid, rid)).id)
      jobs.reload()
    } catch (e) {
      setError(e)
    }
  }
  const cancel = async (job: Job) => {
    setError(undefined)
    try {
      await api.cancelJob(pid, job.id)
      jobs.reload()
    } catch (e) {
      setError(e)
    }
  }
  const storageHint = onOpenTab ? (
    <>в разделе <button type="button" className="export-link" onClick={() => onOpenTab('setup')}>Проект → Хранилище</button></>
  ) : (
    <>в разделе «Проект» → «Хранилище»</>
  )
  const failedHint = (
    <>Если на диске закончилось место — очистите то, что можно пересчитать, {storageHint}, и экспортируйте снова.</>
  )

  if (!reports.data && reports.error) {
    return (
      <StatePanel state="error" title="Не удалось загрузить отчёты">
        <pre className="error-box">{errorMessage(reports.error)}</pre>
        <p>Проверьте, что сервер Монтажа запущен, и повторите.</p>
        <div><button type="button" className="btn" onClick={reports.reload}>Повторить</button></div>
      </StatePanel>
    )
  }
  if (!reports.data) {
    return (
      <div className="export-state" data-state="loading" aria-busy="true">
        <p role="status">Загружаем отчёты…</p>
        <span className="export-sk-button" aria-hidden="true" />
        <span className="skeleton strong export-sk-title" aria-hidden="true" />
        <span className="export-sk-row" aria-hidden="true" />
        <span className="export-sk-row" aria-hidden="true" />
        <span className="export-sk-row" aria-hidden="true" />
      </div>
    )
  }
  if (list.length === 0 && !running && failed) {
    return (
      <StatePanel state="error" title={`${jobTitle(failed)} остановлен`}>
        <pre className="error-box">{failed.error ?? failed.message}</pre>
        <p>{failedHint}</p>
        <div><button type="button" className="btn" disabled={!canStart} onClick={start}>Повторить экспорт</button></div>
        <ErrorText error={error} />
      </StatePanel>
    )
  }
  if (list.length === 0 && !running) {
    return (
      <StatePanel state="empty" title="Экспортов ещё не было">
        <p>
          {rid
            ? 'Экспортируйте текущую версию — после этого здесь появится отчёт с проверками кода и замечаниями модели.'
            : 'Сначала создайте первую версию в разделе «Проект», затем экспортируйте её — здесь появится отчёт с проверками.'}
        </p>
        <div>
          <button type="button" className="btn btn-primary" disabled={!canStart} onClick={start}>Экспортировать текущую версию</button>
        </div>
        <ErrorText error={error} />
      </StatePanel>
    )
  }

  const hint = running
    ? `Текущая — версия ${number ?? '…'}. Пока идёт экспорт, новый не начать.`
    : !rid
      ? 'Сначала создайте первую версию в разделе «Проект».'
      : exported
        ? `Версия ${number ?? '…'} уже экспортирована. Повторный экспорт вернёт готовый ролик.`
        : `Текущая — версия ${number ?? '…'}. Ролик собирается из оригиналов и сразу проверяется.`
  const [latest, ...older] = list

  return (
    <div className="screen export-screen">
      <div className="export-col">
        <section className="export-run" aria-labelledby="export-title">
          <h2 id="export-title" className="section-title">Экспорт</h2>
          {failed && (
            <div className="export-failed" role="alert">
              <h3>{jobTitle(failed)} остановлен</h3>
              <pre className="error-box">{failed.error ?? failed.message}</pre>
              <p>{failedHint}</p>
            </div>
          )}
          <div className="export-run-row">
            <button
              type="button"
              className={canStart ? 'btn btn-primary' : 'btn'}
              disabled={!canStart}
              aria-describedby="export-hint"
              onClick={start}
            >
              Экспортировать текущую версию
            </button>
            <p id="export-hint" className="export-hint">{hint}</p>
          </div>
          {running && <RunningExport job={running} onCancel={() => cancel(running)} />}
          <ErrorText error={error} />
        </section>
        {latest && <ReportView pid={pid} report={latest} job={exports.find((j) => j.id === latest.job_id)} rate={rate} />}
        {older.map((r) => (
          <ReportView key={r.id} pid={pid} report={r} job={exports.find((j) => j.id === r.job_id)} rate={rate} older />
        ))}
      </div>
      <div className="export-side">
        {latest && (
          <HumanCheck
            key={`${latest.id}:${latest.human_status}:${latest.human_at ?? ''}`}
            pid={pid}
            report={latest}
            onSaved={reports.reload}
          />
        )}
        {rid && <ForYouTube pid={pid} rid={rid} number={number} onOpenTab={onOpenTab} />}
      </div>
    </div>
  )
}

/** Субтитры и главы текущей версии для YouTube: файл и текст для описания. */
function ForYouTube({ pid, rid, number, onOpenTab }: {
  pid: string
  rid: string
  number: number | undefined
  onOpenTab?: (tab: Tab) => void
}) {
  const chapters = useAsync(() => api.chapters(pid, rid), [pid, rid])
  const [copied, setCopied] = useState(false)
  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
    }
  }
  const ch = chapters.data
  return (
    <section className="export-human export-youtube" aria-labelledby="yt-title">
      <h2 id="yt-title" className="section-title">Для YouTube · версия {number ?? '…'}</h2>
      <div className="export-yt-part">
        <h3 className="group-title">Субтитры</h3>
        <p className="export-yt-note">Из исправленной расшифровки; вырезанное в них не попадает, время — по монтажу.</p>
        <div><a className="btn btn-sm" href={api.captionsUrl(pid, rid)} download>Скачать субтитры (.srt)</a></div>
      </div>
      <div className="export-yt-part">
        <h3 className="group-title">Главы для описания</h3>
        <ErrorText error={chapters.error} />
        {ch && ch.items.length === 0 && (
          <p className="export-yt-note">
            Глав пока нет. Их ставит Claude или вы на{' '}
            {onOpenTab
              ? <button type="button" className="export-link" onClick={() => onOpenTab('timeline')}>таймлайне</button>
              : 'таймлайне'}
            : «Глава у курсора».
          </p>
        )}
        {ch && ch.items.length > 0 && (
          <>
            <pre className="export-yt-chapters mono">{ch.text}</pre>
            <div className="export-yt-actions">
              <button type="button" className="btn btn-sm" onClick={() => copy(ch.text)}>
                {copied ? 'Скопировано' : 'Скопировать'}
              </button>
              {ch.ok && <span className="mark" data-ok="yes">по правилам YouTube</span>}
            </div>
          </>
        )}
        {ch && ch.problems.length > 0 && (
          <ul className="export-yt-problems" aria-label="Что не так с главами">
            {ch.problems.map((p) => <li key={p} className="mark" data-state="warning">{p}</li>)}
          </ul>
        )}
        {ch?.notes.map((n) => <p key={n} className="export-yt-note">{n}</p>)}
      </div>
    </section>
  )
}

function RunningExport({ job, onCancel }: { job: Job; onCancel: () => void }) {
  const pct = Math.round(job.progress * 100)
  return (
    <div className="export-task" data-state={job.state}>
      <span className="export-task-name">
        <span>{jobTitle(job)}</span>
        <span className="export-task-note mono">{job.message || (job.state === 'queued' ? 'ждёт своей очереди' : '')}</span>
      </span>
      <span className="export-task-progress">
        {job.state === 'running' && (
          <span className="progress" role="progressbar" aria-label="Ход экспорта" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
            <span style={{ width: `${pct}%` }} />
          </span>
        )}
        <span className="mark" data-state={job.state}>
          {JOB_STATE[job.state]}
          {job.state === 'running' && <> · <span className="mono">{pct}%</span></>}
        </span>
      </span>
      <button type="button" className="btn btn-quiet" onClick={onCancel}>отменить</button>
    </div>
  )
}

const HUMAN: Record<QaReport['human_status'], string> = {
  pending: 'ещё нет',
  approved: 'посмотрел, принимаю',
  changes_requested: 'нужны правки',
}

/** older — прежний отчёт: проверки кратко, решение человека строкой (панель справа — для последнего). */
function ReportView({ pid, report, job, rate, older = false }: {
  pid: string
  report: Report
  job: Job | undefined
  rate: Rate | undefined
  older?: boolean
}) {
  const n = reportRevisionNumber(report)
  const { passed, total, reminders } = checksSummary(report)
  const frames = reportFrames(report)
  const duration = frames !== undefined && rate ? fmtUs(frameToUs(frames, rate), 1) : undefined
  const file = job?.output_path ? baseName(job.output_path) : undefined
  const url = api.jobOutputUrl(pid, report.job_id)
  // Финальный и прежние отчёты — без подробностей, проверки в две колонки (макет 10d)
  const compact = report.final || older
  const titleId = `export-report-${report.id}`
  const checksId = `export-checks-${report.id}`

  return (
    <article
      className="export-report"
      data-critical={report.critical ? 'yes' : 'no'}
      data-final={report.final ? 'yes' : 'no'}
      aria-labelledby={titleId}
    >
      <header className="export-report-head">
        <div className="export-report-title">
          <h2 id={titleId} className="section-title">Отчёт · версия {n ?? '?'}</h2>
          <span className="chip" data-final={report.final ? 'yes' : 'no'}>{report.final ? 'финальный' : 'не финальный'}</span>
          {report.stale && <span className="chip" data-stale="yes">устарел: версия больше не текущая</span>}
        </div>
        <p className="export-report-file mono">
          {[file, duration].filter(Boolean).join(' · ')}
          {(file || duration) && ' · '}
          <a href={url} target="_blank" rel="noreferrer">открыть ролик</a>
        </p>
      </header>

      <section className="export-block" aria-labelledby={checksId}>
        <h3 id={checksId} className={report.final ? 'sr-only' : 'export-sub'}>Проверки кода · пройдено {passed} из {total}{reminders > 0 && ` · напоминаний: ${reminders}`}</h3>
        <ol className={compact ? 'export-checks compact' : 'export-checks'}>
          {Object.entries(report.technical).map(([key, check]) => {
            const status = checkStatus(key, check)
            // Напоминание не ошибка: data-ok="yes", но показано отдельно
            const ok = status === 'fail' ? 'no' : 'yes'
            const detail = compact ? '' : checkDetail(key, check)
            return (
              <li key={key} data-ok={ok} data-remind={status === 'remind' ? 'yes' : undefined}>
                {status === 'remind'
                  ? <span className="mark" data-state="warning" aria-hidden="true" />
                  : <span className="mark" data-ok={ok} aria-hidden="true" />}
                <span className="export-check-name">
                  <span>{checkTitle(key)}</span>
                  {detail && <span className="export-check-detail mono">{detail}</span>}
                </span>
                {status === 'remind'
                  ? <span className="mark plain" data-state="warning">не всё проверено</span>
                  : <span className="mark plain" data-ok={ok}>{ok === 'yes' ? 'пройдено' : 'НЕ пройдено'}</span>}
              </li>
            )
          })}
        </ol>
      </section>

      {report.model_notes.length === 0 ? (
        <p className="export-none">Замечаний модели нет.</p>
      ) : (
        <section className="export-block" aria-labelledby={`export-notes-${report.id}`}>
          <h3 id={`export-notes-${report.id}`} className="export-sub">Замечания модели</h3>
          <ul className="export-notes">
            {report.model_notes.map((note, i) => {
              const us = rate ? frameToUs(note.out_start_frame, rate) : undefined
              return (
                <li key={i}>
                  {us === undefined ? (
                    <span className="time-link">кадр {note.out_start_frame}</span>
                  ) : (
                    <a
                      className="time-link"
                      href={`${url}#t=${(us / 1e6).toFixed(2)}`}
                      target="_blank"
                      rel="noreferrer"
                      title="Открыть ролик с этого места"
                    >
                      {fmtUs(us, 1)}
                    </a>
                  )}
                  <p className="read">{note.text}</p>
                </li>
              )
            })}
          </ul>
        </section>
      )}

      {older && (
        <p className="export-report-human">
          Проверка человеком: {HUMAN[report.human_status]}{report.human_note ? ` — ${report.human_note}` : ''}
        </p>
      )}
    </article>
  )
}

type Verdict = 'approved' | 'changes_requested'

function HumanCheck({ pid, report, onSaved }: { pid: string; report: Report; onSaved: () => void }) {
  const n = reportRevisionNumber(report)
  const [editing, setEditing] = useState(report.human_status !== 'approved')
  const [verdict, setVerdict] = useState<Verdict | undefined>(report.human_status === 'pending' ? undefined : report.human_status)
  const [note, setNote] = useState(report.human_note ?? '')
  const [error, setError] = useState<unknown>()
  const [saving, setSaving] = useState(false)
  const save = async (e: FormEvent) => {
    e.preventDefault()
    if (!verdict) return
    setError(undefined)
    setSaving(true)
    try {
      await api.qaHuman(pid, report.id, verdict, note)
      onSaved()
    } catch (err) {
      setError(err)
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="export-human" aria-labelledby="export-human-title">
      <h2 id="export-human-title" className="section-title">Проверка человеком</h2>
      {!editing ? (
        <>
          <p className="mark export-human-verdict" data-state="completed">Посмотрел, принимаю</p>
          {report.human_note && <p className="export-human-note">{report.human_note}</p>}
          {report.human_at && <p className="export-human-date">{fmtShortDate(report.human_at)}</p>}
          <div><button type="button" className="btn btn-quiet" onClick={() => setEditing(true)}>Изменить решение</button></div>
        </>
      ) : (
        <form className="export-human-form" onSubmit={save}>
          <p className="export-human-for">Относится к отчёту версии {n ?? '?'}.</p>
          <fieldset className="export-verdicts">
            <legend className="sr-only">Решение</legend>
            <label>
              <input type="radio" name={`verdict-${report.id}`} checked={verdict === 'approved'} onChange={() => setVerdict('approved')} />
              Посмотрел, принимаю
            </label>
            <label>
              <input
                type="radio"
                name={`verdict-${report.id}`}
                checked={verdict === 'changes_requested'}
                onChange={() => setVerdict('changes_requested')}
              />
              Нужны правки
            </label>
          </fieldset>
          <label className="field">
            <span className="field-label">Заметка</span>
            <textarea rows={4} value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
          <div><button type="submit" className="btn" disabled={!verdict || saving}>Сохранить</button></div>
          <ErrorText error={error} />
        </form>
      )}
    </section>
  )
}
