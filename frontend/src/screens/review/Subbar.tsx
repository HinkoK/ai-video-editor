// Строка под шапкой экрана проверки: состояние слева, клавиши справа.
import type { ReactNode } from 'react'

export function Subbar({ children, keys, notice }: { children: ReactNode; keys?: boolean; notice?: ReactNode }) {
  return (
    <div className="subbar rv-subbar" role="status">
      <p className="rv-status">{children}</p>
      {notice && <span className="rv-notice">{notice}</span>}
      {keys && (
        <ul className="keys" aria-label="Клавиши">
          <li><kbd>↑</kbd><kbd>↓</kbd>правка</li>
          <li><kbd>←</kbd><kbd>→</kbd>стык</li>
          <li><kbd>J</kbd><kbd>K</kbd><kbd>L</kbd>просмотр</li>
          <li><kbd>M</kbd>метка</li>
        </ul>
      )}
    </div>
  )
}
