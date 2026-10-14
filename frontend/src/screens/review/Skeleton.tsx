// Загрузка экрана проверки: очередь и карточка серыми плашками, как в макете.

export function Skeleton({ wide }: { wide?: boolean }) {
  return (
    <div className="rv-body rv-skeleton" data-layout={wide ? 'propose' : 'queue'} aria-hidden="true">
      <div className="rv-skeleton-queue">
        <span className="skeleton strong rv-sk-h" />
        <span className="skeleton rv-sk-row" />
        <span className="skeleton rv-sk-row" />
        <span className="skeleton strong rv-sk-h rv-sk-h2" />
        <span className="skeleton rv-sk-row" />
        <span className="skeleton rv-sk-row" />
        <span className="skeleton rv-sk-row" />
      </div>
      <div className="rv-skeleton-detail">
        <span className="skeleton rv-sk-title" />
        <span className="skeleton rv-sk-line" />
        <div className="rv-sk-players">
          <span className="rv-sk-frame" />
          <span className="rv-sk-frame" />
        </div>
      </div>
    </div>
  )
}
