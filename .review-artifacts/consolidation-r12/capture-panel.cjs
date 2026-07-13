/*
 * Reviewer follow-up capture (attempt 2 fix): AgentStatusFloatingPanel.
 *
 * This is an AC-named combined-risk surface (rs01 A7 primitive region ×
 * design r10/r11 icon-token + chrome pass) that the first pass omitted.
 * Captures the EXPANDED panel for both instances:
 *   - "Agents" (source=managed) — richest: mode-switch + agent-group list
 *   - "Status" (source=manual)  — terminal status list
 * across light+dark × mobile/tablet/desktop, matching baseline calibration
 * (deviceScaleFactor 2; 390/768/1440 viewports).
 *
 * Output shot names (new surfaces, additive to the 9 baseline shots):
 *   08-agent-status-panel-managed
 *   09-agent-status-panel-manual
 *   icon-03-panel-refresh   (element clip of .panel-refresh-icon ↻)
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

async function newPage(browser, theme, vp, mode) {
  const ctx = await browser.newContext({ viewport: VIEWPORTS[vp], deviceScaleFactor: 2 });
  await ctx.addInitScript(({ t, m }) => {
    localStorage.setItem('claude_hub_color_scheme', t);
    localStorage.setItem('claude_hub_app_mode', m);
  }, { t: theme, m: mode });
  const page = await ctx.newPage();
  await page.goto(BASE, { waitUntil: 'networkidle', timeout: 45000 });
  await page.waitForTimeout(1800);
  return { ctx, page };
}

// Expand a floating-status panel by its trigger label ("Agents" or "Status").
async function expandPanel(page, label) {
  // The trigger shows the label text; match the .status-trigger containing it.
  const trigger = page.locator('.status-trigger', { hasText: label }).first();
  if (!(await trigger.count())) return false;
  // If already expanded from a previous open, collapse others first via Escape.
  await trigger.click({ timeout: 6000 }).catch(() => {});
  await page.waitForTimeout(500);
  return (await page.locator('.agent-status[data-expanded="true"] .status-panel').count()) > 0
      || (await page.locator('.status-panel').count()) > 0;
}

async function shot(page, theme, vp, name) {
  await page.screenshot({ path: `${OUT}/${theme}/${vp}/${name}.png` });
  log(`    ✓ ${theme}/${vp}/${name}.png`);
}
async function shotClip(page, theme, vp, name, selector) {
  const el = page.locator(selector).first();
  if (!(await el.count())) { log(`    ⚠ ${name}: ${selector} not found — SKIP`); return false; }
  try { await el.screenshot({ path: `${OUT}/${theme}/${vp}/${name}.png` }); log(`    ✓ ${theme}/${vp}/${name}.png (clip)`); return true; }
  catch (e) { log(`    ⚠ ${name}: clip failed (${e.message.split('\n')[0]}) — SKIP`); return false; }
}

async function capture(browser, theme, vp) {
  // The TabBar (and its two AgentStatusFloatingPanel instances) lives inside
  // .terminal-mode-shell, which is display:none in workspace mode — so the
  // triggers are only laid out / clickable in TERMINAL mode. Both "Status"
  // (manual) and "Agents" (managed) pills are present in the terminal-mode
  // top bar because managed workspace tabs also exist in this session.
  const { ctx, page } = await newPage(browser, theme, vp, 'terminal');
  try {
    // 08 managed "Agents" panel (richest surface)
    if (await expandPanel(page, 'Agents')) {
      await shot(page, theme, vp, '08-agent-status-panel-managed');
      // icon-03 panel-refresh close-up (↻) while panel open
      await shotClip(page, theme, vp, 'icon-03-panel-refresh', '.panel-refresh-icon');
    } else {
      log(`    ⚠ 08: Agents panel did not expand — SKIP`);
    }
    // collapse before opening the other to avoid overlap
    await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(300);
    await page.mouse.click(3, 3).catch(() => {});
    await page.waitForTimeout(300);

    // 09 manual "Status" panel
    if (await expandPanel(page, 'Status')) {
      await shot(page, theme, vp, '09-agent-status-panel-manual');
    } else {
      log(`    ⚠ 09: Status panel did not expand — SKIP`);
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
        await capture(browser, theme, vp);
      }
    }
  } finally {
    await browser.close();
  }
  log('\nPANEL CAPTURE COMPLETE');
})().catch(e => { console.error('FAIL:', e.stack || e.message); process.exit(3); });
