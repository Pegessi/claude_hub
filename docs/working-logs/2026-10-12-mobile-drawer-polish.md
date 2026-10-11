# Mobile Chat drawer polish (2026-10-12)

## Scope

Four phone-facing changes to the Chat drawer and composer: a rightward swipe
anywhere on the main column opens the drawer, the Pinned section folds and no
longer covers the drawer list, Stop and Send/Queue share one composer line, and
the working-session light pauses while the drawer is hidden or dragged.

## Findings

- **Surface swipe.** The existing gesture only listened on a 20px edge band
  because ttyd runs in an iframe whose touches never reach this document. The
  main column now joins as a third gesture zone with its own intent rule (12px
  slop, 1.5:1 horizontal bias, rightward only). Editable fields,
  `[data-drawer-swipe-ignore]` regions and horizontal scrollers that can still
  scroll back keep their gestures; terminal panes still rely on the edge band.
- **"No light on mobile".** A diagnostic page opened in HarmonyOS Huawei
  Browser 6.1.8 (ArkWeb 7, Chrome 144) reported reduced motion off, every
  required CSS feature supported and the beam animating. A LAN preview of the
  branch then showed the light in the drawer on the same phone. The original
  report was not reproduced, and the address the phone normally used for the
  Hub was not confirmed.
- **Pinned overlap.** The pinned group is `position: sticky` with
  `max-height: 40vh`. In a 540px-tall phone viewport four pins fill that block,
  and the workspace header and active Chat scroll underneath it. The drawer now
  lets pins scroll with the list; desktop keeps the sticky section.
- **Composer wrap.** A wrapping flex row breaks lines using each item's
  hypothetical size before any shrinking, so a non-shrinking tools group pushed
  Queue onto its own line once Stop appeared. The tools group now has a zero
  flex basis and only the model trigger truncates.
- **Drag jank.** Measured with Playwright touch input at 4x CPU throttle and
  four working Chats, per drag move: script 0.24ms, style recalc 4.6ms, main
  thread 17.7ms; with the light hidden, style 0.6ms and main thread 3.8ms. The
  Vue update per move was negligible and the dev server only slightly slower
  than production. The beam animates `stroke-dashoffset` on the main thread,
  and the closed drawer is only `visibility: hidden`, so its animations also
  cost 148ms of main thread per second behind every page. Pausing them unless
  the drawer is open and at rest brought a drag move to 6.6ms and a closed
  drawer to 1ms per second.

## Verification

- `./scripts/verify.sh` docs, frontend-lint, frontend-types, frontend-tests
  (760 passed) and frontend-build passed on the branch head.
- Chromium emulation at the phone's 440x540 viewport, DPR 3 and Huawei user
  agent against read-only previews (all non-GET API calls blocked or answered
  from the matching GET): eleven swipe scenarios, pinned scroll and fold, the
  composer at 320, 360, 440 and 1440px, and the light's play state when closed,
  dragging, open and closed again.
- On the reporter's phone the swipe, the light, the composer line and drag
  smoothness were accepted against a LAN preview of the branch.
- Not verified: iPhone Safari, and swipes that start on a terminal iframe. The
  backend is unchanged, so backend tests were not rerun.
