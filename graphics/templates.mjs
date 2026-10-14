// Шаблоны графики. Каждый — HTML поверх кадра на прозрачном фоне.
// Движение: появление (--enter), спокойное чтение, уход (--exit) — в конце
// участка. Текст — как есть, без переделки: команды и названия должны
// совпадать с записью. Проверку полей делает сервер (graphics.py).
//
// Это нейтральный стартовый набор. Свой вид — цвета и шрифты в theme.css,
// свои шаблоны — новые ключи в T (как добавить — docs/STYLE_GUIDE.md, шаг 5).

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c])
const u = (n) => `calc(${n}*var(--u))`

/** Кегль, при котором самое длинное слово влезает в ширину (в единицах --u). */
const wordEm = (w) => [...String(w)].reduce((n, ch) =>
  n + (/[ЖЩШЮМЫФWMЦД]/i.test(ch) ? 0.82 : /[A-ZА-ЯЁ]/.test(ch) ? 0.66 : /\d/.test(ch) ? 0.58 : /[.,:;!?'"«»()…-]/.test(ch) ? 0.3 : 0.55), 0)
const fit = (text, width, max) => Math.min(max, Math.floor(width / Math.max(...String(text ?? '').split(/\s+/).map(wordEm), 0.1)))

const BASE = `
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { width: 100vw; height: 100vh; background: transparent; overflow: hidden; }
body { font-family: var(--font); color: var(--text); -webkit-font-smoothing: antialiased; }
.in { animation: enter var(--enter) var(--ease-in) both, leave var(--exit) var(--ease-out) calc(var(--D) - var(--exit)) forwards; }
@keyframes enter { from { opacity: 0; transform: translateY(calc(14 * var(--u))); } to { opacity: 1; transform: none; } }
@keyframes leave { to { opacity: 0; transform: translateY(calc(8 * var(--u))); } }
.fade { animation: fadein var(--enter) ease-out both, fadeout var(--exit) ease-in calc(var(--D) - var(--exit)) forwards; }
@keyframes fadein { from { opacity: 0; } to { opacity: 1; } }
@keyframes fadeout { to { opacity: 0; } }
.card { background: var(--card); border: 1px solid var(--card-line); border-radius: var(--radius); }
/* Размытие в движении: сильное в начале быстрого движения и гаснет к
   середине. Фильтры — в странице (MB); SVG-фильтр меняется только скачком,
   поэтому ступеней шесть — по ступени на кадр. Анимации — linear */
@keyframes mb-x { 0% { filter: url(#mbx6); } 9% { filter: url(#mbx5); } 18% { filter: url(#mbx4); } 27% { filter: url(#mbx3); }
                  36% { filter: url(#mbx2); } 45% { filter: url(#mbx1); } 54%, 100% { filter: none; } }
@keyframes mb-y { 0% { filter: url(#mby6); } 9% { filter: url(#mby5); } 18% { filter: url(#mby4); } 27% { filter: url(#mby3); }
                  36% { filter: url(#mby2); } 45% { filter: url(#mby1); } 54%, 100% { filter: none; } }
@keyframes mb-z { 0% { filter: url(#mbz6); } 9% { filter: url(#mbz5); } 18% { filter: url(#mbz4); } 27% { filter: url(#mbz3); }
                  36% { filter: url(#mbz2); } 45% { filter: url(#mbz1); } 54%, 100% { filter: none; } }
`

// Перемотка на момент ms — одна для сборки роликов (render.mjs), снимков и
// монитора. Элемент, который начал двигаться, получает свой слой
// (will-change): Chrome двигает его на доли пикселя. Без слоя текст встаёт на
// целые пиксели, и в хвосте плавного торможения стоит кадр-два, потом
// прыгает — «подлагивает» (замер 12.10: разница кадров 1, 39, 0, 41 вместо
// 61, 37, 22, 12). Слой остаётся до конца: снятие слоя перерисовывает текст
// иначе — толчок на кадр. Снимается только у элемента, который вырос из
// малого размера (scale < 1 в начале): слой мог растрироваться мелким, и
// неподвижный кадр вышел бы размытым
const SEEK = `<script>window.__seek = (ms) => {
  const moving = new Map()
  for (const a of document.getAnimations()) {
    a.pause()
    // hold: ухода нет — графика целиком до последнего кадра (следующая встык или PAN)
    if (window.__hold && /^(leave|fadeout)/.test(a.animationName ?? '')) { a.currentTime = 0; continue }
    a.currentTime = ms
    const t = a.effect.getComputedTiming()
    const el = a.effect.target
    if (!el || t.progress === null || t.progress <= 0 || t.progress >= 1) continue
    const grows = a.effect.getKeyframes().some((k) => /scale\\((0?\\.\\d+|0)[,)\\s]/.test(String(k.transform ?? '')))
    moving.set(el, (moving.get(el) ?? false) || grows)
  }
  for (const el of document.querySelectorAll('[data-wc="grow"]')) if (!moving.has(el)) { el.style.willChange = ''; delete el.dataset.wc }
  for (const [el, grows] of moving) if (!el.dataset.wc) { el.style.willChange = 'transform'; el.dataset.wc = grows ? 'grow' : '1' }
  for (const a of window.__adapters ?? []) a.seek(ms)
}</script>`

// Фильтры размытия в движении (keyframes mb-x, mb-y, mb-z в BASE): по
// горизонтали, вертикали и во все стороны. Сила — от высоты кадра: на 1440p
// такая же, как на 1080p
const MB = (h) => {
  const k = h / 1080
  const f = (id, sx, sy) => `<filter id="${id}" x="-50%" y="-50%" width="200%" height="200%" color-interpolation-filters="sRGB">` +
    `<feGaussianBlur stdDeviation="${(sx * k).toFixed(1)} ${(sy * k).toFixed(1)}"/></filter>`
  return '<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>' +
    [22, 16, 11, 7, 4, 2].map((v, i) => f(`mbx${6 - i}`, v, 0)).join('') +
    [16, 12, 8, 5, 3, 1.5].map((v, i) => f(`mby${6 - i}`, 0, v)).join('') +
    [7, 5, 3.5, 2.2, 1.2, 0.6].map((v, i) => f(`mbz${6 - i}`, v, v)).join('') + '</defs></svg>'
}


const T = {
  // Подпись: продукт, версия, роль — внизу слева
  lower_third: (p) => ({
    css: `.lt { position: absolute; left: var(--margin); bottom: var(--margin); max-width: ${u(1300)}; padding: ${u(22)} ${u(30)}; }
          .lt b { display: block; font-size: ${u(46)}; font-weight: 700; color: var(--text); }
          .lt span { display: block; margin-top: ${u(6)}; font-size: ${u(30)}; color: var(--text-2); }
          .lt i { position: absolute; left: 0; top: ${u(18)}; bottom: ${u(18)}; width: ${u(6)}; background: var(--accent); border-radius: ${u(3)}; }`,
    html: `<div class="lt card in"><i></i><b>${esc(p.title)}</b>${p.subtitle ? `<span>${esc(p.subtitle)}</span>` : ''}</div>`,
  }),
  // Термин и короткое объяснение — справа по центру
  term: (p) => ({
    css: `.tm { position: absolute; right: var(--margin); top: 50%; width: ${u(780)}; transform: translateY(-50%); }
          .tm .card { padding: ${u(34)} ${u(40)}; }
          .tm b { display: block; font-size: ${u(fit(p.term, 690, 54))}; font-weight: 700; color: var(--accent); }
          .tm p { margin-top: ${u(14)}; font-size: ${u(32)}; line-height: 1.45; }`,
    html: `<div class="tm"><div class="card in"><b>${esc(p.term)}</b><p>${esc(p.definition)}</p></div></div>`,
  }),
  // Акцент одной мысли — крупно, внизу по центру
  keyword: (p) => ({
    css: `.kw { position: absolute; left: 50%; bottom: ${u(150)}; transform: translateX(-50%); max-width: ${u(1500)}; }
          .kw div { padding: ${u(18)} ${u(34)}; font-size: ${u(60)}; font-weight: 700; line-height: 1.2; text-align: center; }`,
    html: `<div class="kw"><div class="card in">${esc(p.text)}</div></div>`,
  }),
  // Карточка главы — затемнение и название
  chapter: (p) => ({
    css: `.ch { position: absolute; inset: 0; background: var(--dim); display: grid; place-items: center; }
          .ch div { text-align: center; max-width: ${u(1700)}; }
          .ch small { display: block; font-size: ${u(34)}; font-weight: 600; color: var(--accent); letter-spacing: .04em; }
          .ch b { display: block; margin-top: ${u(14)}; font-size: ${u(fit(p.title, 1700, 110))}; font-weight: 700; line-height: 1.1; }`,
    html: `<div class="ch fade"><div class="in">${p.number ? `<small>Глава ${esc(p.number)}</small>` : ''}<b>${esc(p.title)}</b></div></div>`,
  }),
  // Пометка «ожидание сокращено» — сверху справа, честно о времени
  time_compressed: (p) => ({
    css: `.tc { position: absolute; right: var(--margin); top: var(--margin); display: flex; align-items: center; gap: ${u(12)};
             padding: ${u(12)} ${u(20)}; font-size: ${u(28)}; color: var(--text); }
          .tc i { display: inline-flex; gap: ${u(2)}; }
          .tc i::before, .tc i::after { content: ""; border-left: ${u(14)} solid var(--warn);
             border-top: ${u(9)} solid transparent; border-bottom: ${u(9)} solid transparent; }`,
    html: `<div class="tc card in"><i></i>${esc(p.text || 'ожидание сокращено')}</div>`,
  }),
  // Схема этапов: текущий подсвечен, пройденные отмечены
  steps: (p) => {
    const active = Number.isInteger(p.active) ? p.active : -1
    const items = (p.steps || []).map((s, i) => {
      const state = i < active ? 'done' : i === active ? 'now' : 'next'
      return `<li data-state="${state}"><span>${i < active ? '' : i + 1}</span>${esc(s)}</li>`
    }).join('')
    return {
      css: `.st { position: absolute; left: var(--margin); top: 50%; transform: translateY(-50%); width: ${u(820)}; }
            .st .card { padding: ${u(34)} ${u(40)}; }
            .st h2 { font-size: ${u(36)}; font-weight: 700; margin-bottom: ${u(20)}; }
            .st ol { list-style: none; display: flex; flex-direction: column; gap: ${u(16)}; }
            .st li { display: flex; align-items: center; gap: ${u(18)}; font-size: ${u(36)}; color: var(--text-2); }
            .st li span { flex: none; width: ${u(48)}; height: ${u(48)}; display: grid; place-items: center; border-radius: 50%;
                          border: ${u(2)} solid var(--card-line); font-size: ${u(24)}; font-family: var(--mono); }
            .st li[data-state="now"] { color: var(--text); font-weight: 600; }
            .st li[data-state="now"] span { border-color: var(--accent); background: var(--accent); color: var(--on-accent); }
            .st li[data-state="done"] span { border-color: var(--ok); }
            .st li[data-state="done"] span::after { content: ""; width: ${u(10)}; height: ${u(18)};
               border-right: ${u(3)} solid var(--ok); border-bottom: ${u(3)} solid var(--ok); transform: translateY(-15%) rotate(45deg); }`,
      html: `<div class="st"><div class="card in">${p.title ? `<h2>${esc(p.title)}</h2>` : ''}<ol>${items}</ol></div></div>`,
    }
  },
  // Сравнение двух вариантов по одним и тем же пунктам
  compare: (p) => {
    const col = (c) => `<div class="cc"><h3>${esc(c.title)}</h3><ul>${(c.items || []).map((x) => `<li>${esc(x)}</li>`).join('')}</ul></div>`
    return {
      css: `.cp { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); width: ${u(1500)}; }
            .cp .card { padding: ${u(40)} ${u(48)}; }
            .cp h2 { font-size: ${u(40)}; font-weight: 700; margin-bottom: ${u(26)}; }
            .cp .cols { display: grid; grid-template-columns: 1fr 1fr; gap: ${u(40)}; }
            .cp h3 { font-size: ${u(34)}; font-weight: 700; color: var(--accent); margin-bottom: ${u(14)}; }
            .cp ul { list-style: none; display: flex; flex-direction: column; gap: ${u(10)}; }
            .cp li { font-size: ${u(30)}; line-height: 1.4; padding-left: ${u(24)}; position: relative; }
            .cp li::before { content: ""; position: absolute; left: 0; top: 0.55em; width: ${u(10)}; height: ${u(10)};
               border-radius: 50%; background: var(--accent); }
            .cp p { margin-top: ${u(24)}; font-size: ${u(26)}; color: var(--text-2); }`,
      html: `<div class="cp"><div class="card in">${p.title ? `<h2>${esc(p.title)}</h2>` : ''}<div class="cols">${col(p.left)}${col(p.right)}</div>${p.note ? `<p>${esc(p.note)}</p>` : ''}</div></div>`,
    }
  },
  // Чек-лист: сделано / сейчас / впереди
  checklist: (p) => ({
    css: `.cl { position: absolute; left: var(--margin); top: 50%; transform: translateY(-50%); width: ${u(820)}; }
          .cl .card { padding: ${u(34)} ${u(40)}; }
          .cl h2 { font-size: ${u(36)}; font-weight: 700; margin-bottom: ${u(20)}; }
          .cl ul { list-style: none; display: flex; flex-direction: column; gap: ${u(16)}; }
          .cl li { display: flex; align-items: center; gap: ${u(16)}; font-size: ${u(34)}; }
          .cl li::before { content: ""; flex: none; width: ${u(30)}; height: ${u(30)}; border-radius: ${u(6)};
             border: ${u(3)} solid var(--text-2); }
          .cl li[data-state="done"]::before { border-color: var(--ok); background: var(--ok); }
          .cl li[data-state="now"]::before { border-color: var(--accent); }
          .cl li[data-state="todo"] { color: var(--text-2); }`,
    html: `<div class="cl"><div class="card in">${p.title ? `<h2>${esc(p.title)}</h2>` : ''}<ul>${(p.items || []).map((i) => `<li data-state="${esc(i.state)}">${esc(i.text)}</li>`).join('')}</ul></div></div>`,
  }),
  // Команда крупно — ровно как в записи, моноширинным
  command: (p) => ({
    css: `.cm { position: absolute; left: 50%; bottom: ${u(140)}; transform: translateX(-50%); max-width: ${u(1700)}; }
          .cm .card { padding: ${u(24)} ${u(34)}; }
          .cm pre { font-family: var(--mono); font-size: ${u(40)}; line-height: 1.4; white-space: pre-wrap; word-break: break-all; }
          .cm pre::before { content: "$ "; color: var(--text-2); }
          .cm span { display: block; margin-top: ${u(10)}; font-size: ${u(24)}; color: var(--text-2); }`,
    html: `<div class="cm"><div class="card in"><pre>${esc(p.text)}</pre>${p.caption ? `<span>${esc(p.caption)}</span>` : ''}</div></div>`,
  }),
  // Вставка страницы во весь кадр: снимок (capture_page) в рамке на фоне
  // сцены, медленно прокручивается. top — с какой доли высоты снимка начать,
  // scroll — на сколько высот кадра прокрутить, zoom и cx — крупнее и куда по
  // ширине смотреть; zoom < 1 — колонка посередине с полями цвета страницы
  page_insert: (p) => {
    const W = 1632
    const zoom = p.zoom ?? 1
    const imgW = W * zoom
    const imgH = (imgW * p.h) / p.w
    const top = (p.top ?? 0) * imgH
    const startY = top > 0 ? 0 : 64 // страница с начала — видны верхние скруглённые углы
    const scroll = (p.scroll ?? 0.15) * 1080
    const shift = imgW >= W ? -(imgW - W) * (p.cx ?? 0.5) : (W - imgW) / 2
    const src = /^(https?:|\/api\/)/.test(p.src) ? p.src : 'file://' + String(p.src).split('/').map(encodeURIComponent).join('/')
    const bg = /^#[0-9a-f]{6}$/i.test(p.bg ?? '') ? p.bg : '#ffffff' // фон страницы — поля рамки
    return {
      css: `.pbg { position: absolute; inset: 0; background: var(--x-bg); }
            .pw { position: absolute; inset: 0; ${p.enter === 'up' ? 'animation: pi-up 380ms var(--ease-in) both;' : ''} }
            .pc { position: absolute; left: ${u(144)}; top: 0; width: ${u(W)}; height: ${u(Math.max(imgH - top, 1200))};
                  border-radius: ${u(16)}; overflow: hidden; background: ${bg}; transform-origin: 50% 0;
                  animation: pi-scroll var(--D) linear both, leave-cut 80ms linear calc(var(--D) - 120ms) forwards; }
            .pc img { display: block; width: ${u(imgW)}; max-width: none; margin-left: ${u(shift)}; margin-top: ${u(-top)}; }
            @keyframes pi-scroll { from { transform: translateY(${u(startY)}) scale(1); }
                                   to { transform: translateY(${u(startY - scroll)}) scale(1.025); } }
            @keyframes pi-up { from { transform: translateY(${u(1080)}); } to { transform: none; } }
            @keyframes leave-cut { to { opacity: 0; } }`,
      html: `<div class="pbg"></div><div class="pw"><div class="pc"><img src="${esc(src)}"></div></div>`,
    }
  },
}

export const TEMPLATES = Object.keys(T)

/** Страница графики: шрифты из пакета, тема, шаблон, длительность. */
export function page(template, props, { durationMs, fontsCss, themeCss, base, height }) {
  const t = T[template]
  if (!t) throw new Error(`нет шаблона ${template}`)
  const { css, html } = t(props, { durationMs })
  // Положение, выставленное автором на мониторе (как Motion в Premiere): сдвиг в
  // долях кадра и масштаб от центра — поверх любого шаблона
  const pl = props.place
  const body = pl ? `<div style="position:absolute;inset:0;transform-origin:50% 50%;transform:translate(${pl.x * 100}vw,${pl.y * 100}vh) scale(${pl.scale})">${html}</div>` : html
  const filters = MB(height ?? 1080)
  const under = underlay(props)
  // base — папка graphics/: относительные пути шаблонов (картинки, свои скрипты)
  return `<!doctype html><html><head><meta charset="utf-8">${base ? `<base href="${base}">` : ''}<style>${fontsCss}\n${themeCss}\n:root{--D:${durationMs}ms}\n${BASE}\n${under.css}\n${css}</style></head><body>${filters}${under.html}${body}${props.hold === true ? '<script>window.__hold = true</script>' : ''}${SEEK}</body></html>`
}

// Начало и конец графики во весь кадр: чтобы лицо не мелькало между двумя
// графиками и не было «новый кадр и сразу затемнение». Под шаблоном —
// сплошной фон сцены (--x-bg):
// - enter: cut — фон с первого кадра, без затемнения поверх лица; pan — то же,
//   а въезд справа и уход кадра под графикой влево делает сборка
//   (backend/src/longform/render/select.py);
// - hold: true — ухода нет: графика целиком (и фон) до последнего кадра —
//   следующая встаёт встык или уводит её переходом PAN.
// Без полей — как раньше, подложки нет.
function underlay(props) {
  const cut = props.enter === 'cut' || props.enter === 'pan'
  const hold = props.hold === true
  if (!cut && !hold) return { css: '', html: '' }
  const anim = [cut ? '' : 'xu-in 160ms linear both', hold ? '' : 'leave-xu 260ms linear calc(var(--D) - 300ms) forwards']
    .filter(Boolean).join(', ')
  return {
    css: `.xu { position: absolute; inset: 0; background: var(--x-bg);${anim ? ` animation: ${anim};` : ''} }
          @keyframes xu-in { from { opacity: 0; } }
          @keyframes leave-xu { to { opacity: 0; } }`,
    html: '<div class="xu"></div>',
  }
}
