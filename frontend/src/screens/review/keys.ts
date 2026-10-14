// Горячие клавиши экрана проверки. Клавиша — физическая (e.code), чтобы
// работали и в русской раскладке. Не срабатывают, когда фокус в поле ввода.
import { useEffect, useRef } from 'react'

export type ScreenKey = 'up' | 'down' | 'left' | 'right' | 'enter' | 'back' | 'j' | 'k' | 'l' | 'm'

const CODES: Record<string, ScreenKey> = {
  ArrowUp: 'up',
  ArrowDown: 'down',
  ArrowLeft: 'left',
  ArrowRight: 'right',
  Enter: 'enter',
  NumpadEnter: 'enter',
  Backspace: 'back',
  KeyJ: 'j',
  KeyK: 'k',
  KeyL: 'l',
  KeyM: 'm',
}

function typing(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false
  // Флажок — не поле ввода: после щелчка по нему стрелки должны работать
  return el.isContentEditable ||
    el.matches('textarea, select, input:not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"])')
}

/** Подписка на клавиши, пока экран открыт. handler возвращает true, если
 *  клавиша обработана (тогда действие браузера по умолчанию отменяется). */
export function useScreenKeys(handler: (key: ScreenKey, target: HTMLElement | null) => boolean): void {
  const ref = useRef(handler)
  useEffect(() => {
    ref.current = handler
  })
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || typing(e.target)) return
      const key = CODES[e.code]
      if (!key) return
      // Зажатая клавиша не должна решить подряд несколько правок
      if (e.repeat && (key === 'enter' || key === 'back' || key === 'm')) return
      const target = e.target instanceof HTMLElement ? e.target : null
      // ↵ на кнопке или ссылке нажимает её саму — не перехватываем,
      // кроме строк очереди: там ↵ значит «Оставить»
      if (key === 'enter' && target?.closest('button, a, summary, [role="slider"]') && !target.closest('[data-queue-item]')) return
      if (ref.current(key, target)) e.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
}
