// Проверка размещения графики: влезает ли текст в кадр и не налезает ли
// друг на друга — на любых допустимых полях (шаблоны должны быть
// универсальными).
//
//   node fitcheck.mjs <cases.json>
//
// cases: {width, height, cases: [{name, template, props, durationMs}]}.
// Каждую графику Chrome собирает так же, как render.mjs, проматывает на
// момент чтения (все появления закончились, уход не начался) и меряет каждую
// строку текста и каждую картинку. Печатает JSON [{name, problems: [...]}]:
// строка вылезла за кадр с полями, две строки разных элементов пересеклись,
// текст лёг на картинку.
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { tmpdir } from 'node:os'
import { fileURLToPath, pathToFileURL } from 'node:url'
import puppeteer from 'puppeteer-core'
import { page as buildPage } from './templates.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const spec = JSON.parse(readFileSync(process.argv[2], 'utf8'))
const MARGIN = spec.margin ?? 24 // поле от края кадра, точки

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

const fonts = fontsCss()
const theme = readFileSync(join(here, 'theme.css'), 'utf8')
const dir = mkdtempSync(join(tmpdir(), 'fitcheck-'))
const chrome = process.env.CHROME ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: ['--no-first-run', '--allow-file-access-from-files'] })
const out = []
try {
  const tab = await browser.newPage()
  await tab.setViewport({ width: spec.width, height: spec.height, deviceScaleFactor: 1 })
  for (const [i, c] of spec.cases.entries()) {
    const file = join(dir, `${i}.html`)
    writeFileSync(file, buildPage(c.template, c.props, { durationMs: c.durationMs, fontsCss: fonts, themeCss: theme,
      base: `${pathToFileURL(here).href}/` }))
    await tab.goto(pathToFileURL(file).href, { waitUntil: 'load' })
    await tab.evaluate(() => document.fonts.ready)
    await tab.evaluate(() => Promise.all((window.__adapters ?? []).map((a) => a.ready)))
    const problems = await tab.evaluate((margin) => {
      // Момент чтения: как в render.mjs
      let from = 0
      let to = Infinity
      for (const a of document.getAnimations()) {
        const t = a.effect.getComputedTiming()
        if (/^(leave|fadeout)/.test(a.animationName ?? '')) to = Math.min(to, t.delay)
        else from = Math.max(from, t.delay + t.activeDuration)
      }
      const at = Math.min(from + 1, to === Infinity ? from + 1 : to - 1)
      for (const a of document.getAnimations()) { a.pause(); a.currentTime = at }
      const W = innerWidth
      const H = innerHeight
      const boxes = []
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        if (!n.textContent.trim()) continue
        const el = n.parentElement
        const cs = getComputedStyle(el)
        if (cs.visibility === 'hidden' || +cs.opacity === 0 || cs.color === 'rgba(0, 0, 0, 0)') continue
        const r = document.createRange()
        r.selectNodeContents(n)
        // Блок, которому принадлежит строка: ближайший предок не строчный
        let block = el
        while (block && getComputedStyle(block).display.startsWith('inline')) block = block.parentElement
        for (const b of r.getClientRects()) {
          if (b.width < 1 || b.height < 1) continue
          boxes.push({ kind: 'text', block, text: n.textContent.trim().slice(0, 30), x0: b.left, y0: b.top, x1: b.right, y1: b.bottom })
        }
      }
      for (const img of document.querySelectorAll('img')) {
        const b = img.parentElement.getBoundingClientRect() // видимая часть — рамка
        boxes.push({ kind: 'image', block: img, text: 'картинка', x0: b.left, y0: b.top, x1: b.right, y1: b.bottom })
      }
      const problems = []
      for (const b of boxes) {
        if (b.kind === 'text' && (b.x0 < margin || b.y0 < margin || b.x1 > W - margin || b.y1 > H - margin)) {
          problems.push(`за краем кадра: «${b.text}» [${Math.round(b.x0)},${Math.round(b.y0)}–${Math.round(b.x1)},${Math.round(b.y1)}]`)
        }
      }
      // Строки разных блоков не пересекаются (строки одного блока стоят рядом по построению)
      for (let a = 0; a < boxes.length; a++) {
        for (let c = a + 1; c < boxes.length; c++) {
          const p = boxes[a]
          const q = boxes[c]
          if (p.block === q.block || p.block.contains(q.block) || q.block.contains(p.block)) continue
          if (p.kind === 'image' && q.kind === 'image') continue
          const ox = Math.min(p.x1, q.x1) - Math.max(p.x0, q.x0)
          const oy = Math.min(p.y1, q.y1) - Math.max(p.y0, q.y0)
          if (ox > 3 && oy > 3) problems.push(`налезает: «${p.text}» и «${q.text}»`)
        }
      }
      return [...new Set(problems)]
    }, MARGIN)
    out.push({ name: c.name, problems })
  }
} finally {
  await browser.close()
}
console.log(JSON.stringify(out))
