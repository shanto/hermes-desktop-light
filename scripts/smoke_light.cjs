#!/usr/bin/env node
/**
 * Native launch smoke test for the remote-only ("light") Windows payload.
 *
 * Launches the packaged executable twice with a deliberately crippled PATH (the
 * application directory plus the Windows system directories, nothing else), so a
 * build that quietly borrowed Python, Node or Git from the runner cannot pass.
 * It asserts:
 *   - the app reports itself as packaged
 *   - it honours HERMES_DESKTOP_USER_DATA_DIR instead of reaching outside it
 *   - the in-app updater reports external ownership and is unsupported
 *   - renderer state (localStorage) survives a relaunch
 *
 * usage: node smoke_light.cjs <upstreamCheckout> <executablePath> <outputDir>
 */
'use strict'

const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { createRequire } = require('node:module')

const [sourceArg, executableArg, outputArg] = process.argv.slice(2)
if (!sourceArg || !executableArg || !outputArg) {
  console.error('usage: node smoke_light.cjs <upstreamCheckout> <executablePath> <outputDir>')
  process.exit(2)
}

const source = path.resolve(sourceArg)
const executablePath = path.resolve(executableArg)
const output = path.resolve(outputArg)
fs.mkdirSync(output, { recursive: true })

if (!fs.existsSync(executablePath)) {
  console.error(`packaged executable is missing: ${executablePath}`)
  process.exit(2)
}

// Playwright is a devDependency of the upstream Desktop workspace; pnpm does not
// hoist its transitive playwright package, so resolve it from that workspace.
const upstreamRequire = createRequire(path.join(source, 'apps', 'desktop', 'package.json'))
let driver = null
let driverName = ''
for (const name of ['@playwright/test', 'playwright', 'playwright-core']) {
  try {
    driver = upstreamRequire(name)
    driverName = name
    break
  } catch (error) {
    void error
  }
}
if (!driver || !driver._electron) {
  console.error('no Playwright Electron driver is available in the upstream workspace')
  process.exit(2)
}

const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-light-smoke-'))
const systemRoot = process.env.SystemRoot || 'C:\\Windows'
const env = {
  ...process.env,
  HERMES_HOME: path.join(scratch, 'home'),
  HERMES_DESKTOP_USER_DATA_DIR: path.join(scratch, 'user-data'),
  PATH: [path.dirname(executablePath), path.join(systemRoot, 'System32'), systemRoot].join(path.delimiter),
}
delete env.ELECTRON_RUN_AS_NODE
for (const key of Object.keys(env)) {
  if (key !== 'PATH' && key.toLowerCase() === 'path') delete env[key]
}

const checks = {
  playwright: driverName,
  runtimePath: 'application directory and Windows system directories only',
  executable: path.basename(executablePath),
  node: process.version,
  launches: [],
}

async function launchAndCheck(attempt) {
  const app = await driver._electron.launch({ executablePath, env, timeout: 90000 })
  try {
    const page = await app.firstWindow({ timeout: 90000 })
    await page.waitForFunction(() => document.body.innerText.trim().length > 20, { timeout: 90000 })

    assert.equal(await app.evaluate(({ app: electronApp }) => electronApp.isPackaged), true,
      'packaged build did not report app.isPackaged')
    assert.equal(await app.evaluate(({ app: electronApp }) => electronApp.getPath('userData')),
      env.HERMES_DESKTOP_USER_DATA_DIR, 'packaged build ignored HERMES_DESKTOP_USER_DATA_DIR')

    const bridge = await page.evaluate(() => ({
      present: typeof window.hermesDesktop === 'object' && window.hermesDesktop !== null,
      check: typeof window.hermesDesktop?.updates?.check === 'function',
      apply: typeof window.hermesDesktop?.updates?.apply === 'function',
    }))
    assert.equal(bridge.present, true, 'preload bridge window.hermesDesktop is missing')
    assert.equal(bridge.check, true, 'updater bridge window.hermesDesktop.updates.check is missing')

    const status = await page.evaluate(() => window.hermesDesktop.updates.check({ force: true }))
    assert.equal(status?.mechanism, 'external',
      `in-app updater does not report external ownership: ${JSON.stringify(status)}`)
    assert.equal(status?.supported, false,
      `in-app updater reports a supported self-update path: ${JSON.stringify(status)}`)

    let apply = null
    if (bridge.apply) {
      apply = await page.evaluate(() => window.hermesDesktop.updates.apply())
      assert.equal(apply?.mechanism, 'external',
        `updater apply does not report external ownership: ${JSON.stringify(apply)}`)
    }

    if (attempt === 0) {
      await page.evaluate(() => window.localStorage.setItem('hermes-light-smoke', 'retained'))
      await page.screenshot({ path: path.join(output, 'smoke.png') }).catch(() => {})
    } else {
      assert.equal(await page.evaluate(() => window.localStorage.getItem('hermes-light-smoke')),
        'retained', 'renderer state did not survive a relaunch')
    }

    checks.launches.push({
      attempt,
      updaterCheck: status,
      updaterApply: apply,
      userData: env.HERMES_DESKTOP_USER_DATA_DIR,
    })
  } finally {
    await app.close()
  }
}

;(async () => {
  for (let attempt = 0; attempt < 2; attempt++) {
    await launchAndCheck(attempt)
  }
  checks.localStorageRetained = true
  fs.writeFileSync(path.join(output, 'native-checks.json'),
    JSON.stringify(checks, null, 2) + '\n', 'utf8')
  console.log('native smoke passed: two launches, restricted PATH, external updater, state retained')
})().catch(error => {
  console.error(error)
  process.exitCode = 1
})
