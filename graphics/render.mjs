// Отрисовка графики в кадры PNG с прозрачностью.
//
//   node render.mjs <spec.json> <папка для кадров>
//
// spec: {template, props, width, height, fps, frames}. Анимации CSS ставятся
// на паузу и проматываются на время каждого кадра (Web Animations API), так
// что кадры точные и не зависят от скорости машины. В конце печатает JSON
// {frames, ms}. Chrome — установленный (переменная CHROME меняет путь).
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import puppeteer from 'puppeteer-core'
import { page as buildPage } from './templates.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const [specPath, outDir] = process.argv.slice(2)
if (!specPath || !outDir) {
  console.error('использование: node render.mjs <spec.json> <папка для кадров>')
  process.exit(2)
}
const spec = JSON.parse(readFileSync(specPath, 'utf8'))
const { template, props, width, height, fps, frames } = spec
mkdirSync(outDir, { recursive: true })

// Шрифты — из пакета, без интернета: подставляем file:// пути в их CSS
function fontsCss() {
  const css = []
  const packs = [['golos-text', [400, 500, 600, 700]], ['jetbrains-mono', [400, 500]]]
  for (const [pkg, weights] of packs) {
    const dir = join(here, 'node_modules', '@fontsource', pkg)
    for (const w of weights) {
      css.push(readFileSync(join(dir, `${w}.css`), 'utf8').replace(/url\(\.\/files\//g, `url(${pathToFileURL(join(dir, 'files'))}/`))
    }
  }
  return css.join('\n')
}

// Длительность графики в ролике: кадров рисуется с запасом, а уход должен
// закончиться к её последнему кадру
const durationMs = spec.duration_ms ?? (frames / fps) * 1000
const html = buildPage(template, props, { durationMs, height, fontsCss: fontsCss(), themeCss: readFileSync(join(here, 'theme.css'), 'utf8'),
  base: `${pathToFileURL(here).href}/` })
const chrome = process.env.CHROME ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const started = Date.now()
const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: ['--no-first-run', '--allow-file-access-from-files', '--hide-scrollbars'] })
try {
  const tab = await browser.newPage()
  await tab.setViewport({ width, height, deviceScaleFactor: 1 })
  // Страница с диска: шрифты file:// грузятся только так
  const htmlPath = join(outDir, 'page.html')
  writeFileSync(htmlPath, html)
  await tab.goto(pathToFileURL(htmlPath).href, { waitUntil: 'load' })
  await tab.evaluate(() => document.fonts.ready)
  // Анимации не на CSS (Lottie): шаблон регистрирует window.__adapters
  // {ready, seek(ms)} — ждём загрузки, на каждом кадре проматываем (как Hyperframes)
  await tab.evaluate(() => Promise.all((window.__adapters ?? []).map((a) => a.ready)))
  // Между концом всех появлений и началом ухода картинка неподвижна:
  // рисуем её один раз и повторяем файл — долгая графика не рисуется
  // кадр за кадром
  const [stillFrom, stillTo] = await tab.evaluate(() => {
    let from = 0
    let to = Infinity
    for (const a of document.getAnimations()) {
      const t = a.effect.getComputedTiming()
      const name = a.animationName ?? ''
      if (/^(leave|fadeout)/.test(name)) to = Math.min(to, t.delay)
      else from = Math.max(from, t.delay + t.activeDuration)
    }
    // Бесконечная анимация — неподвижного отрезка нет. Infinity
    // нельзя вернуть из evaluate: в массиве он приходит как null, а t >= null
    // истинно — и все кадры стали бы копией первого
    const NEVER = 1e12
    // Lottie играет всё время — неподвижного отрезка нет
    if (window.__adapters?.length) return [NEVER, NEVER]
    return [Math.min(from, NEVER), Math.min(to === Infinity ? from : to, NEVER)]
  })
  let still = null
  for (let i = 0; i < frames; i++) {
    const t = (i / fps) * 1000
    const file = join(outDir, `${String(i).padStart(5, '0')}.png`)
    if (still && t >= stillFrom && t < stillTo) {
      copyFileSync(still, file)
      continue
    }
    // Перемотка — общая функция страницы (templates.mjs, SEEK): ещё и слой у
    // того, что сейчас движется, — без него текст в торможении «подлагивает»
    await tab.evaluate((ms) => window.__seek(ms), t)
    await tab.screenshot({ path: file, omitBackground: true })
    if (!still && t >= stillFrom && t < stillTo) still = file
  }
} finally {
  await browser.close()
}
console.log(JSON.stringify({ frames, ms: Date.now() - started }))
