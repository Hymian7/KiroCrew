/** Render the production first-run gate at three review states for PR #13888. */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const out = process.argv[3] || '../.github/screenshots/pr-13888'
mkdirSync(out, { recursive: true })

const status = {
  platform: 'Linux', installed: false, authenticated: false, ready: false,
  initial_setup_complete: false, repair_required: false, bundled_cli: false,
  docs_url: 'https://kiro.dev/cli/', login_command: 'kiro-cli login',
  sso_login_command: 'kiro-cli login --use-device-flow --license pro',
  setup_allowed: true, sandbox_unavailable: false, sandbox_backend_available: true,
  sandbox_failure_kind: '', sandbox_detail: '', sandbox_remedy: '',
  missing_agent_specs: [], agent_spec_repair_error: '',
}
const claude = {
  id: 'claude', policy_id: 'claude', selectable: true, independent_setup: true,
  installed: 'missing', missing_components: ['claude'],
  install_command: 'npm install -g @zed-industries/claude-code-acp',
  restart_required: false,
}
const codex = {
  id: 'codex', policy_id: 'codex', selectable: true, independent_setup: true,
  installed: 'installed', missing_components: [], install_command: '',
  restart_required: false,
}
const json = (route, body, code = 200) => route.fulfill({
  status: code, contentType: 'application/json', body: JSON.stringify(body),
})

const browser = await chromium.launch()
try {
  for (const scenario of ['probe-failure', 'recheck-failure', 'sandbox-blocked'].filter(
    item => !process.argv[4] || process.argv[4] === item,
  )) {
    const page = await browser.newPage({ viewport: { width: 1400, height: 1000 }, deviceScaleFactor: 2 })
    page.on('pageerror', error => { throw error })
    await page.route(url => new URL(url).pathname.startsWith('/api/'), route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/kiro-prerequisite') {
        return json(route, scenario === 'sandbox-blocked'
          ? { ...status, sandbox_blocked_backends: ['codex'] }
          : status)
      }
      if (path === '/api/config/kirocrew') {
        return json(route, { agent: scenario === 'sandbox-blocked' ? { acp_backend: 'codex' } : {} })
      }
      if (path === '/api/acp-backends') {
        return scenario === 'probe-failure'
          ? json(route, { error: 'Probe unavailable', code: 'probe_unavailable' }, 503)
          : json(route, { backends: scenario === 'sandbox-blocked' ? [codex] : [claude] })
      }
      if (path === '/api/acp-backends/recheck') {
        return json(route, { error: 'Probe unavailable', code: 'probe_unavailable' }, 503)
      }
      return json(route, {})
    })
    await page.goto(`${base}/capture/setup-check-probe-error.html?theme=dark`)
    if (scenario !== 'sandbox-blocked') {
      await page.getByRole('button', { name: 'Use other coding agents' }).click()
    }
    if (scenario === 'probe-failure') {
      const notice = page.getByTestId('other-agents-probe-error')
      await notice.getByRole('button', { name: 'Try again' }).waitFor()
    } else if (scenario === 'recheck-failure') {
      await page.getByTestId('other-agent-detail').getByRole('button', { name: 'Check again', exact: true }).click()
      const notice = page.getByTestId('other-agent-recheck-error')
      await notice.getByText('Could not re-check Claude Code. Press Check again.').waitFor()
      if (await notice.getByRole('button', { name: 'Try again' }).count()) {
        throw new Error('Duplicate retry control is present')
      }
    } else {
      await page.getByRole('radio', { name: 'codex' }).check()
      const detail = page.getByTestId('other-agent-detail')
      await detail.getByText('Sandbox unavailable').waitFor()
      await detail.getByRole('button', { name: 'Copy command' }).waitFor()
      if (await detail.getByRole('button', { name: /Use codex/ }).isEnabled()) {
        throw new Error('Sandbox-blocked agent action is enabled')
      }
      await detail.scrollIntoViewIfNeeded()
    }
    await page.screenshot({ path: `${out}/${scenario}.png`, fullPage: true })
    console.log(`${scenario}: captured current gate`)
    await page.close()
  }
} finally {
  await browser.close()
}
