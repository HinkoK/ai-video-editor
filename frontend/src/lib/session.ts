// Вход: longform-open открывает страницу с #key=... во фрагменте адреса.
// Фрагмент не уходит на сервер и не попадает в журнал. Ключ один раз
// меняется на HttpOnly cookie, после чего фрагмент стирается из адреса и
// истории — в странице ключа больше нет.
import { api, ApiError } from './api'

export type SessionState = 'ok' | 'no_key'

export async function bootstrapSession(): Promise<SessionState> {
  const match = window.location.hash.match(/key=([^&]+)/)
  if (match) {
    await api.exchangeKey(decodeURIComponent(match[1]))
    window.history.replaceState(null, '', window.location.pathname + window.location.search)
  }
  try {
    await api.session()
    return 'ok'
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return 'no_key'
    throw e
  }
}
