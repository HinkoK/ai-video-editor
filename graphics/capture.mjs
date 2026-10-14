// Снимок страницы для вставки в ролик (шаблон page_insert).
//
//   node capture.mjs <ссылка> <снимок.png> [ширина CSS] [наибольшая высота CSS] [фраза]
//
// Chrome без окна открывает страницу в светлой теме и по-русски, прячет
// плашки про cookie, плавающие кнопки и шапку, прокручивает до конца снимка (ленивые картинки) и
// снимает верх страницы в двойной чёткости. Интернет нужен только здесь:
// при сборке ролика снимок берётся с диска. Печатает JSON {title, width, height, bg}.
// С фразой — ещё marks: где на снимке её строки (доли ширины и высоты снимка,
// [x0, y0, x1, y1] на строку) — для своего шаблона-маркера; null — не нашлась.
import puppeteer from 'puppeteer-core'

const [url, out, cssWidth = '1280', maxHeight = '4000', phrase = ''] = process.argv.slice(2)
if (!url || !out) {
  console.error('использование: node capture.mjs <ссылка> <снимок.png> [ширина] [высота]')
  process.exit(2)
}
const width = Number(cssWidth)
const chrome = process.env.CHROME ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const browser = await puppeteer.launch({
  executablePath: chrome, headless: true, args: ['--no-first-run', '--hide-scrollbars', '--lang=ru-RU'],
})
try {
  const tab = await browser.newPage()
  await tab.setViewport({ width, height: 900, deviceScaleFactor: 2 })
  await tab.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }])
  await tab.setExtraHTTPHeaders({ 'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8' })
  const res = await tab.goto(url, { waitUntil: 'networkidle2', timeout: 45000 })
  if (!res || !res.ok()) {
    console.error(`страница не открылась: ${res ? `код ${res.status()}` : 'нет ответа'}`)
    process.exit(3)
  }
  // Плашки про cookie и согласие закрывают текст
  await tab.addStyleTag({
    content: '[id*="cookie" i], [class*="cookie" i], [id*="consent" i], [class*="consent" i], ' +
      '[aria-label*="cookie" i], [aria-modal="true"] { display: none !important; }',
  })
  // Всё закреплённое прячем: окна согласия (бывают без «cookie» в классах),
  // чат и плавающие кнопки, шапку сайта — во вставке только сама страница
  await tab.evaluate(() => {
    for (const el of document.querySelectorAll('body *')) {
      const pos = getComputedStyle(el).position
      if (pos === 'fixed' || pos === 'sticky') el.style.setProperty('display', 'none', 'important')
    }
  })
  const height = await tab.evaluate(async (limit) => {
    const h = () => Math.min(document.documentElement.scrollHeight, limit)
    for (let y = 0; y < h(); y += 600) {
      window.scrollTo(0, y)
      await new Promise((r) => setTimeout(r, 120))
    }
    window.scrollTo(0, 0)
    await document.fonts.ready
    return h()
  }, Number(maxHeight))
  await new Promise((r) => setTimeout(r, 500))
  await tab.screenshot({ path: out, clip: { x: 0, y: 0, width, height }, captureBeyondViewport: true })
  // Цвет фона страницы — им во вставке закрашиваются поля рамки вокруг колонки
  const bg = await tab.evaluate(() => {
    for (const el of [document.body, document.documentElement]) {
      const c = getComputedStyle(el).backgroundColor
      if (c && c !== 'transparent' && c !== 'rgba(0, 0, 0, 0)') return c
    }
    return 'rgb(255, 255, 255)'
  })
  // Фраза на снимке: текст страницы без лишних пробелов, регистра и типографских
  // кавычек; первое вхождение — прямоугольники строк (соседние куски одной строки
  // склеены), в долях снимка
  const marks = phrase ? await tab.evaluate((needle, w, h) => {
    const fold = (c) => ({ '’': "'", '‘': "'", '“': '"', '”': '"', '«': '"', '»': '"', 'ё': 'е', ' ': ' ' })[c] ?? c
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
    const chars = [] // [узел, смещение] на каждый знак нормализованного текста
    let norm = ''
    let space = true
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const el = n.parentElement
      if (!el || !el.getClientRects().length) continue
      const t = n.textContent ?? ''
      for (let i = 0; i < t.length; i++) {
        const c = fold(t[i].toLowerCase())
        if (/\s/.test(c)) {
          if (space) continue
          space = true
          norm += ' '
        } else {
          space = false
          norm += c
        }
        chars.push([n, i])
      }
    }
    const target = [...needle.toLowerCase()].map(fold).join('').replace(/\s+/g, ' ').trim()
    const at = norm.indexOf(target)
    if (at < 0 || !target) return null
    const [sn, so] = chars[at]
    const [en, eo] = chars[at + target.length - 1]
    const range = document.createRange()
    range.setStart(sn, so)
    range.setEnd(en, eo + 1)
    const lines = []
    for (const r of range.getClientRects()) {
      if (r.width < 1 || r.height < 1) continue
      const y = r.top + window.scrollY
      const line = lines.find((l) => Math.abs((l[1] + l[3]) / 2 - (y + r.height / 2)) < r.height / 2)
      if (line) {
        line[0] = Math.min(line[0], r.left); line[1] = Math.min(line[1], y)
        line[2] = Math.max(line[2], r.right); line[3] = Math.max(line[3], y + r.height)
      } else lines.push([r.left, y, r.right, y + r.height])
    }
    return lines.filter((l) => l[3] <= h).map(([x0, y0, x1, y1]) => [x0 / w, y0 / h, x1 / w, y1 / h].map((v) => +v.toFixed(5)))
  }, phrase, width, height) : undefined
  console.log(JSON.stringify({ title: await tab.title(), width: width * 2, height: height * 2, bg,
    ...(phrase ? { marks: marks?.length ? marks : null } : {}) }))
} finally {
  await browser.close()
}
