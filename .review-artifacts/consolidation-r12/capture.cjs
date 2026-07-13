/*
 * COMBINED-state Playwright capture harness (consolidation-r12).
 *
 * Transient verification tool — NOT added to frontend/package.json. Uses the
 * npx-cached Playwright (1.61.1) driving SYSTEM Google Chrome via channel:'chrome'
 * (ms-playwright chromium download is incomplete on this host).
 *
 * Reproduces the r12 baseline capture contract exactly:
 *   - viewports: mobile 390x844, tablet 768x1024, desktop 1440x960
 *   - deviceScaleFactor: 2  (baseline PNGs are 2x: 780/1536/2880 wide)
 *   - themes: light + dark  (data-theme via localStorage claude_hub_color_scheme)
 *   - mode: terminal / workspace via localStorage claude_hub_app_mode
 *   - 7 full-viewport surfaces + 2 element-clip icon close-ups = 9 shots per combo
 *
 * Baseline shot names (must match for 1:1 comparison):
 *   01-terminal-mode, 02-tab-menu-popover, 03-switch-env-modal,
 *   04-env-preset-manager-modal, 05-workspace-mode,
 *   06-workspace-switch-env-modal, 07-workspace-env-preset-manager-modal,
 *   icon-01-tab-menu-trigger, icon-02-pane-refresh
 *
 * EXCLUDED (protected human path e1a9ba7b): toast/notification/network-error.
 * We never trigger a toast; no error-feedback surface is captured.
 */
const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:5199/';
const OUT = '/Users/bytedance/claude_hub-consolidate-r12/.review-artifacts/consolidation-r12/screenshots';

const VIEWPORTS = {
  mobile: { width: 390, height: 844 },
  tablet: { width: 768, height: 1024 },
  desktop: { width: 1440, height: 960 },
};
const THEMES = ['light', 'dark'];
const BREAKPOINTS = ['mobile', 'tablet', 'desktop'];

const log = (...a) => console.log(...a);
const sleep = (p, ms) => p.waitForTimeout(ms);

async function newPage(browser, theme, vp, mode) {
  const ctx = await browser.newContext({
    viewport: VIEWPORTS[vp],
    deviceScaleFactor: 2,
  });
  await ctx.addInitScript(({ t, m }) => {
    localStorage.setItem('claude_hub_color_scheme', t);
    localStorage.setItem('claude_hub_app_mode', m);
  }, { t: theme, m: mode });
  const page = await ctx.newPage();
  await page.goto(BASE, { waitUntil: 'networkidle', timeout: 45000 });
  await sleep(page, 1800);
  return { ctx, page };
}

// Close any open overlay/popover so the next surface starts clean.
async function dismiss(page) {
  await page.keyboard.press('Escape').catch(() => {});
  await sleep(page, 250);
  // click a neutral corner to close popovers that ignore Escape
  await page.mouse.click(3, 3).catch(() => {});
  await sleep(page, 250);
}

async function shot(page, theme, vp, name) {
  const path = `${OUT}/${theme}/${vp}/${name}.png`;
  await page.screenshot({ path });
  log(`    ✓ ${theme}/${vp}/${name}.png`);
}

async function shotClip(page, theme, vp, name, selector) {
  const el = page.locator(selector).first();
  const cnt = await el.count();
  if (!cnt) { log(`    ⚠ ${name}: selector not found (${selector}) — SKIP`); return false; }
  try {
    await el.screenshot({ path: `${OUT}/${theme}/${vp}/${name}.png` });
    log(`    ✓ ${theme}/${vp}/${name}.png (clip)`);
    return true;
  } catch (e) {
    log(`    ⚠ ${name}: clip failed (${e.message.split('\n')[0]}) — SKIP`);
    return false;
  }
}

async function captureTerminal(browser, theme, vp) {
  const { ctx, page } = await newPage(browser, theme, vp, 'terminal');
  try {
    // 01 terminal mode (baseline shell)
    await shot(page, theme, vp, '01-terminal-mode');

    // icon-01 tab-menu-trigger close-up (⋯) — element clip
    await shotClip(page, theme, vp, 'icon-01-tab-menu-trigger', '.tab-menu-trigger');

    // icon-02 pane-refresh (↻) — TerminalPane refresh button. Present in terminal mode.
    await shotClip(page, theme, vp, 'icon-02-pane-refresh', '.pane-action-button');

    // 02 tab-menu popover: open the tab menu on the first tab
    await page.locator('.tab-menu-trigger').first().click({ timeout: 8000 }).catch(() => {});
    await sleep(page, 500);
    await shot(page, theme, vp, '02-tab-menu-popover');

    // 03 switch-env modal (TabBar): the "Switch Env / Model…" menu item only
    // exists for claude tabs. Find a claude tab's trigger; if none, open via any
    // tab-menu that shows the item.
    let opened = false;
    const triggers = page.locator('.tab-menu-trigger');
    const n = await triggers.count();
    for (let i = 0; i < n && !opened; i++) {
      await dismiss(page);
      await triggers.nth(i).click({ timeout: 5000 }).catch(() => {});
      await sleep(page, 400);
      const item = page.locator('.tab-menu-item', { hasText: 'Switch Env' }).first();
      if (await item.count()) {
        await item.click({ timeout: 5000 }).catch(() => {});
        await sleep(page, 700);
        if (await page.locator('.switch-env-modal').count()) opened = true;
      }
    }
    if (opened) {
      await shot(page, theme, vp, '03-switch-env-modal');

      // 04 env-preset-manager modal: click "Manage" inside switch-env modal
      const manage = page.locator('.env-manage-button', { hasText: 'Manage' }).first();
      if (await manage.count()) {
        await manage.click({ timeout: 5000 }).catch(() => {});
        await sleep(page, 900); // async chunk load
        if (await page.locator('.env-manage-modal').count()) {
          await shot(page, theme, vp, '04-env-preset-manager-modal');
        } else {
          log('    ⚠ 04: env-manage-modal did not appear — SKIP');
        }
      } else {
        log('    ⚠ 04: Manage button not found — SKIP');
      }
    } else {
      log('    ⚠ 03: no claude tab with Switch Env item — SKIP 03+04');
    }
  } finally {
    await ctx.close();
  }
}

async function captureWorkspace(browser, theme, vp) {
  const { ctx, page } = await newPage(browser, theme, vp, 'workspace');
  try {
    // 05 workspace mode
    await shot(page, theme, vp, '05-workspace-mode');

    // 06 workspace switch-env modal: click an agent's switch-env button
    const swBtn = page.locator('.agent-status-switch-env').first();
    if (await swBtn.count()) {
      await swBtn.click({ timeout: 8000 }).catch(() => {});
      await sleep(page, 800);
      if (await page.locator('.switch-env-modal').count()) {
        await shot(page, theme, vp, '06-workspace-switch-env-modal');

        // 07 workspace env-preset-manager modal
        const manage = page.locator('.env-manage-button, .tool-button.env-manage-button', { hasText: 'Manage' }).first();
        if (await manage.count()) {
          await manage.click({ timeout: 5000 }).catch(() => {});
          await sleep(page, 900);
          if (await page.locator('.env-manage-modal').count()) {
            await shot(page, theme, vp, '07-workspace-env-preset-manager-modal');
          } else {
            log('    ⚠ 07: env-manage-modal did not appear — SKIP');
          }
        } else {
          log('    ⚠ 07: Manage button not found — SKIP');
        }
      } else {
        log('    ⚠ 06: switch-env-modal did not appear — SKIP 06+07');
      }
    } else {
      log('    ⚠ 06: no agent-status-switch-env button — SKIP 06+07');
    }
  } finally {
    await ctx.close();
  }
}

(async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    for (const theme of THEMES) {
      for (const vp of BREAKPOINTS) {
        log(`\n=== ${theme} / ${vp} ===`);
        await captureTerminal(browser, theme, vp);
        await captureWorkspace(browser, theme, vp);
      }
    }
  } finally {
    await browser.close();
  }
  log('\nCAPTURE COMPLETE');
})().catch(e => { console.error('CAPTURE FAIL:', e.stack || e.message); process.exit(3); });
