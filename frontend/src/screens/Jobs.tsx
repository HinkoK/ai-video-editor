// Фоновые задачи: ход, ошибки, отмена; состояние исполнителя.
// Оформление — styles/jobs.css.
import { useState } from 'react'
import { ErrorText, JobStatus, StatePanel, errorMessage } from '../components/common'
import { api } from '../lib/api'
import { useAsync, useInterval } from '../lib/hooks'
import { hasOpenableOutput, isActiveJob, jobPurpose, jobTitle } from '../lib/jobInfo'
import type { Rate } from '../lib/time'
import type { Project, WorkerStatus } from '../lib/types'

export function Jobs({ project }: { project: Project }) {
  const pid = project.id
  const jobs = useAsync(() => api.jobs(pid), [pid])
  const worker = useAsync(() => api.worker(), [])
  const [error, setError] = useState<unknown>()
  const active = (jobs.data ?? []).some(isActiveJob)
  useInterval(() => { jobs.reload(); worker.reload() }, 2000, active || !worker.data?.alive)
  const rate: Rate | undefined = project.fps_num && project.fps_den ? { num: project.fps_num, den: project.fps_den } : undefined
  const dead = !!worker.data && !worker.data.alive

  const cancel = async (jid: string) => {
    setError(undefined)
    try {
      await api.cancelJob(pid, jid)
      jobs.reload()
    } catch (e) {
      setError(e)
    }
  }

  if (!jobs.data && !jobs.error) {
    return (
      <div className="jobs-state" data-state="loading" aria-busy="true">
        <p role="status">Спрашиваем исполнителя о задачах…</p>
        <span className="jobs-sk-row" aria-hidden="true" />
        <span className="jobs-sk-row" aria-hidden="true" />
        <span className="jobs-sk-row" aria-hidden="true" />
      </div>
    )
  }
  if (jobs.data?.length === 0 && !dead) {
    return (
      <StatePanel state="empty" title="Задач нет">
        <p>
          Задачи появятся, когда вы запустите лёгкую копию или расшифровку в Проекте, пробный ролик «После» в Проверке или
          экспорт.
        </p>
      </StatePanel>
    )
  }

  return (
    <div className="screen jobs-screen" data-state={dead ? 'error' : undefined}>
      <div className="jobs-head">
        <h2 className="section-title">Задачи</h2>
        <p role="status" className="jobs-worker">
          Исполнитель:
          <WorkerChip worker={worker.data} failed={!!worker.error && !worker.data} />
        </p>
      </div>

      {dead && worker.data && (
        <div role="alert" className="jobs-alert">
          <p className="jobs-alert-title">
            {worker.data.heartbeat_age_s === null
              ? 'Задачи не идут: исполнитель не запускался.'
              : `Задачи не идут: исполнитель не отвечает ${Math.round(worker.data.heartbeat_age_s)} с.`}
          </p>
          <p>
            Остановите сервер Монтажа в терминале (<kbd>Ctrl C</kbd>) и запустите той же командой,{' '}
            <code>uv run longform-server</code>. Задачи, которые шли, исполнитель запустит снова сам.
          </p>
        </div>
      )}

      {jobs.error && !jobs.data && (
        <div role="alert" className="jobs-alert">
          <p className="jobs-alert-title">Не удалось загрузить задачи</p>
          <pre className="error-box">{errorMessage(jobs.error)}</pre>
          <div><button type="button" className="btn" onClick={jobs.reload}>Повторить</button></div>
        </div>
      )}
      <ErrorText error={error ?? (jobs.data ? jobs.error : undefined)} />

      {jobs.data && jobs.data.length > 0 && (
        <table className="table jobs-table">
          <thead>
            <tr>
              <th scope="col">Задача</th>
              <th scope="col">Для чего</th>
              <th scope="col" className="jobs-col-state">Состояние</th>
              <th scope="col" className="jobs-col-tries">Попыток</th>
              <th scope="col" className="jobs-col-act"><span className="sr-only">Действие</span></th>
            </tr>
          </thead>
          <tbody>
            {jobs.data.map((j) => (
              <tr key={j.id} data-state={j.state}>
                <td>{jobTitle(j)}</td>
                <td className="jobs-purpose">{jobPurpose(j, project.assets, rate)}</td>
                <td><JobStatus job={j} /></td>
                <td className="jobs-col-tries mono">{j.attempts}</td>
                <td>
                  {isActiveJob(j) ? (
                    <button type="button" className="btn btn-quiet" onClick={() => cancel(j.id)}>отменить</button>
                  ) : hasOpenableOutput(j) ? (
                    <a className="btn btn-quiet" href={api.jobOutputUrl(pid, j.id)} target="_blank" rel="noreferrer">открыть</a>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function WorkerChip({ worker, failed }: { worker: WorkerStatus | undefined; failed: boolean }) {
  if (failed) return <span className="chip" data-state="error">сервер не отвечает</span>
  if (!worker) return <span className="chip" data-state="queued">спрашиваем…</span>
  return worker.alive ? (
    <span className="chip" data-state="ok" title={worker.pid ? `процесс ${worker.pid}` : undefined}>работает</span>
  ) : (
    <span className="chip" data-state="error">не отвечает — перезапустите longform-server</span>
  )
}
