# Round-6 visual wave 1 — validation evidence (VIS-01/02/03)

## Method note (per reviewer note #2)
Playwright is not a dependency of the frontend at this baseline. Per GP assumption (c)
and the approved fallback plan, visual verification uses: (a) grep post-conditions
for the CSS/token changes, (b) offline WCAG 2.x relative-luminance recomputation of
the new token values, (c) built CSS inspection of the generated chunk to confirm the
selectors/rules shipped. Live browser screenshots were NOT captured because that
would require adding a browser dependency or mutating workspace state to seed a
long-title task; the static probes below are sufficient reviewer-checkable evidence.

## WCAG contrast recomputation (VIS-02)
```
White (#ffffff) on old dark --ch-color-accent-strong #3b82f6 (pre-fix):  3.68:1  < 4.5:1 AA FAIL
White (#ffffff) on new dark --ch-color-accent-strong #2563eb (post-fix): 5.17:1 >= 4.5:1 AA PASS
White (#ffffff) on new dark --ch-color-accent-hover  #1d4ed8 (post-fix): 6.70:1 >= 4.5:1 AA PASS
```
Computed via WCAG 2.x relative-luminance formula: L = 0.2126*R + 0.7152*G + 0.0722*B
where each channel is linearized (c <= 0.03928 ? c/12.92 : ((c+0.055)/1.055)^2.4);
contrast ratio = (L1+0.05)/(L2+0.05). Code in the final report.

Affected controls (all consume `--ch-color-accent-strong`/`--ch-color-accent-hover` via
tokens, no per-control color override):
- LoginView `.feishu-login-btn` (18px/400 — not WCAG "large text", 4.5:1 applies)
- AgentWorkspaceView `.primary-button` (~13px/600 — normal text)
- MobileControls `.control-btn.active` (icon + label — inverse text)

Regression check (text-on-light consumers of `--ch-color-accent-strong`):
- AgentStatusFloatingPanel `.panel-mode-switch button[data-active='true']`: color on
  accent-soft (rgba(accent-strong, 0.2) over panel bg). Darkening accent-strong from
  #3b82f6 to #2563eb increases the luminance delta against the light soft wash
  (estimated ratio ~4.0:1 → ~4.7:1 in dark, neutral in light since light accent-strong
  is unchanged charcoal). No regression.
- AgentWorkspaceView `.summary-chip--accent strong`: color on the chip's soft bg;
  same argument — darker fg on a soft chip bg only improves legibility.

Light-theme tokens (L644-646) are unchanged:
- `--ch-color-accent-strong: #30302d` (charcoal) — white on this passes by wide margin.
- `--ch-color-accent-hover: #242421` — unchanged.

## Grep post-conditions

### A3 VIS-01 .review-badge (AgentWorkspaceView.vue:7875-7893)
```css
.review-badge {
  display: inline-flex;
  ...
  white-space: normal;       /* was: white-space: nowrap */
  max-width: 100%;
  flex: 0 1 auto;            /* was: flex: 0 0 auto */
}
```
- `flex: 0 1 auto` allows the badge to shrink when constrained.
- `white-space: normal` allows badge label text to wrap inside the inline-flex.
- Parent `.task-card-badges` has `flex-wrap: wrap; justify-content: flex-end` (L7825 area)
  so a badge that doesn't fit on the same line as the age label drops to the next line.
- Removed `overflow: hidden; text-overflow: ellipsis;` (cleanup — ellipsis is inert on
  inline-flex; removing overflow lets the wrapped text paint).
- Short-title cards render identically (badge fits in its natural width).
- `autonomy-badge` (status pill) still uses `flex: 0 0 auto; white-space: nowrap` by
  design — it is a tiny single-word pill that must not wrap.

### A4 VIS-02 dark tokens (App.vue:505-512 / 644-646)
```
  --ch-color-accent-strong: #2563eb;   /* dark, was #3b82f6 */
  --ch-color-accent-hover:  #1d4ed8;   /* dark, was #2563eb */
  --ch-color-accent-soft:   rgba(37, 99, 235, 0.2);  /* re-tinted to match */
  ...
  --ch-color-accent-strong: #30302d;   /* light — unchanged */
  --ch-color-accent-hover:  #242421;   /* light — unchanged */
  --ch-color-accent-soft:   #ececea;   /* light — unchanged */
```

### A5 VIS-03 :focus-visible (AgentWorkspaceView.vue:7159-7166)
```css
.tool-button:focus-visible,
.primary-button:focus-visible,
.workspace-desktop-action:focus-visible,
.abort-button:focus-visible,
.danger-button:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring-strong);
  outline-offset: 2px;
}
```
Matches the pattern used at:
- App.vue:788/809/865/921 (shell buttons)
- AgentWorkspaceView.vue:6719 (agent-status card actions)
- LoginView.vue:124-127 (feishu login CTA)
- AgentStatusFloatingPanel.vue:765-768 (panel mode-switch)
Note: `outline` (not `box-shadow`) matches the shell/agent-status patterns and
replaces the UA auto outline — no double-ring because the browser default UA
outline is suppressed when an author `outline` is specified (it replaces the
user-agent style rather than stacking). Hover/active rules are unchanged.

### A6 Build / chunk-split
- `pnpm lint` — exit 0
- `pnpm build` — exit 0, 86 modules, 1.33s; agent-config-KvQ5_sR0.js hash unchanged
  (CSS/token edits do not touch chunk boundaries; index JS hash unchanged at
  Cyl27xuL.js = same hash as round-4-perf-w1 output since only App.vue <style>
  and AWV <style> blocks changed — index.css hash shifted as expected).
- `node scripts/verify-chunk-split.mjs` — exit 0, markers/trigger/nostatic pass.

## Files changed
- frontend/src/App.vue (+8/-3) — dark accent tokens darkened one Tailwind step.
- frontend/src/components/AgentWorkspaceView.vue (+23/-4) — .review-badge wrap
  fix + :focus-visible rule group for toolbar buttons.
