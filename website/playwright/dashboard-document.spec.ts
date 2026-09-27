import { test, expect } from '@playwright/test'
import { buildSync } from 'esbuild'
import { fileURLToPath } from 'node:url'

// Exercise the production builder in Chromium: selector engines in DOM mocks
// do not reproduce SVG's namespaced attributes. No gateway or credentials needed.
test.use({ storageState: { cookies: [], origins: [] } })
const bundle = buildSync({
  entryPoints: [fileURLToPath(new URL('../src/pages/chat/command-center/dashboardDocument.ts', import.meta.url))],
  bundle: true, write: false, format: 'iife', globalName: 'taskDashboard',
}).outputFiles[0].text

for (const kind of ['html', 'svg', 'xlink'] as const) {
  for (const href of ['https://outside.invalid/synthetic', '/relative', '']) {
    test(`published view blocks ${kind} navigation to ${JSON.stringify(href)}`, async ({ page }) => {
      let document = ''
      const attempted: string[] = []
      await page.route('**/*', async route => {
        const url = route.request().url()
        if (url === 'https://dashboard.invalid/host') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><body></body>' })
        if (url === 'https://dashboard.invalid/published' && !document) return route.abort()
        if (url === 'https://dashboard.invalid/published') return route.fulfill({ contentType: 'text/html', body: document })
        attempted.push(url)
        return route.abort()
      })
      await page.goto('https://dashboard.invalid/host')
      await page.addScriptTag({ content: bundle })
      const link = kind === 'html' ? `<a id="go" href="${href}">Open</a>`
        : `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="200" height="60"><a id="go" ${kind === 'xlink' ? 'xlink:href' : 'href'}="${href}"><text x="0" y="30">Open</text></a></svg>`
      document = await page.evaluate(html => (window as unknown as { taskDashboard: { dashboardDocument: (html: string, vars: object, mode: string) => string } }).taskDashboard.dashboardDocument(html, {}, 'light'), `${link}<p id="keep">Synthetic task</p>`)
      await page.evaluate(() => {
        const iframe = document.createElement('iframe')
        iframe.setAttribute('sandbox', '')
        iframe.src = 'https://dashboard.invalid/published'
        document.body.append(iframe)
      })
      const frame = page.frameLocator('iframe')
      await expect(frame.locator('#keep')).toBeVisible()
      // Start observing before the click. Abort every synthetic outbound request;
      // the browser can navigate its own opaque iframe despite an empty sandbox.
      const navigation = page.waitForRequest(r => r.isNavigationRequest(), { timeout: 1000 }).then(r => r.url(), () => null)
      await frame.locator('#go').click()
      expect(await navigation).toBeNull()
      expect(attempted).toEqual([])
      expect(await frame.locator('#go').evaluate(el => Array.from(el.attributes).filter(a => a.localName === 'href').map(a => a.value))).toEqual([])
      await expect(frame.locator('#keep')).toBeVisible()
    })
  }
}

test('published view preserves supported SVG fragments, CSS and disclosures', async ({ page }) => {
  await page.addScriptTag({ content: bundle })
  const result = await page.evaluate(() => {
    const builder = (window as unknown as { taskDashboard: { dashboardDocument: (html: string, vars: object, mode: string) => string } }).taskDashboard
    const html = builder.dashboardDocument('<style>.map{display:grid}</style><article class="map"><details><summary>Evidence</summary>Passed</details><a href="#target">Jump</a><svg xmlns:xlink="http://www.w3.org/1999/xlink"><defs><path id="shape" d="M0 0L10 10" /></defs><use xlink:href="#shape"/><a href="#target"><text>Jump</text></a><a xlink:href="#target"><text>Linked fragment</text></a></svg><p id="target">Target</p><script>bad()</script><form><input></form><svg><animate attributeName="href" to="https://outside.invalid"/><set attributeName="href" to="https://outside.invalid"/></svg></article>', {}, 'light')
    const parsed = new DOMParser().parseFromString(html, 'text/html')
    return {
      links: Array.from(parsed.querySelectorAll('*')).flatMap(el => Array.from(el.attributes).filter(a => a.localName === 'href').map(a => a.value)),
      path: parsed.querySelector('defs path')?.getAttribute('d'),
      disclosure: parsed.querySelector('details summary')?.textContent,
      css: html.includes('.map{display:grid}'),
      forbidden: parsed.querySelectorAll('script,form,input,animate,set').length,
      csp: parsed.head.firstElementChild?.getAttribute('content'),
    }
  })
  // The existing sanitizer strips <use>; this repair does not widen its allowlist.
  expect(result.links).toEqual(['#target', '#target', '#target'])
  expect(result.path).toBe('M0 0L10 10')
  expect(result.disclosure).toBe('Evidence')
  expect(result.css).toBe(true)
  expect(result.forbidden).toBe(0)
  expect(result.csp).toContain("script-src 'none'")
})
