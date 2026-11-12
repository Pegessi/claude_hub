<template>
  <!-- Game palettes render their own coherent chrome icon family; the five
       neutral palettes render the caller's fallback slot (or nothing for
       game-only decorative badges) so neutral DOM and layout stay unchanged. -->
  <svg
    v-if="gamePalette"
    :class="[
      'theme-chrome-icon',
      `theme-chrome-icon--${kind}`,
      `theme-chrome-icon--${gamePalette}`,
      { flipped },
    ]"
    :data-theme-icon="kind"
    :data-icon-family="gamePalette"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    aria-hidden="true"
  >
    <!-- ====================================================================
         ZENLESS (ZZZ) — Analog CRT, cassette reels, halftone & signal stickers
         ==================================================================== -->
    <template v-if="gamePalette === 'zenless'">
      <template v-if="kind === 'appearance'">
        <rect
          x="2.5"
          y="5"
          width="19"
          height="14"
          rx="3.5"
          stroke="currentColor"
          stroke-width="2.1"
        />
        <circle
          cx="8.5"
          cy="12"
          r="2.6"
          fill="currentColor"
        />
        <circle
          cx="15.5"
          cy="12"
          r="2.6"
          fill="currentColor"
        />
        <path
          d="M7 5L4.5 2M17 5L19.5 2M6.5 16.5h11"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
        />
      </template>
      <template v-else-if="kind === 'terminal'">
        <rect
          x="2.5"
          y="5.5"
          width="19"
          height="14.5"
          rx="3.5"
          stroke="currentColor"
          stroke-width="2.1"
        />
        <path
          d="M8.5 5.5L6 2.2M15.5 5.5L18 2.2M6.8 10.2l3.2 2.5-3.2 2.5M12.2 15.2h4.6"
          stroke="currentColor"
          stroke-width="2.1"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
      </template>
      <template v-else-if="kind === 'workspace'">
        <rect
          x="2"
          y="5"
          width="20"
          height="14"
          rx="3"
          stroke="currentColor"
          stroke-width="2.1"
        />
        <path
          d="M2 9.2h20"
          stroke="currentColor"
          stroke-width="1.8"
        />
        <circle
          cx="8.2"
          cy="14.2"
          r="2.3"
          fill="currentColor"
        />
        <circle
          cx="15.8"
          cy="14.2"
          r="2.3"
          fill="currentColor"
        />
      </template>
      <template v-else-if="kind === 'chats'">
        <path
          d="M4 4.5h16a2 2 0 0 1 2 2v8.5a2 2 0 0 1-2 2H11l-4.8 3.5V17H4a2 2 0 0 1-2-2V6.5a2 2 0 0 1 2-2Z"
          stroke="currentColor"
          stroke-width="2.1"
          stroke-linejoin="round"
        />
        <path
          d="M12.8 7.2 9.5 11h3l-1.3 3.6 4.3-4.2h-3l1.3-3.2Z"
          fill="currentColor"
        />
      </template>
      <template v-else-if="kind === 'search'">
        <circle
          cx="10.5"
          cy="10.5"
          r="6.5"
          stroke="currentColor"
          stroke-width="2.2"
        />
        <circle
          cx="10.5"
          cy="10.5"
          r="2"
          fill="currentColor"
        />
        <path
          d="M15.5 15.5 21 21M10.5 2v2M10.5 17v2M2 10.5h2M17 10.5h2"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
        />
      </template>
      <template v-else-if="kind === 'archive'">
        <rect
          x="3"
          y="4.5"
          width="18"
          height="15"
          rx="3"
          stroke="currentColor"
          stroke-width="2.1"
        />
        <path
          d="M3 9.5h18"
          stroke="currentColor"
          stroke-width="1.9"
        />
        <circle
          cx="9"
          cy="14.5"
          r="1.9"
          fill="currentColor"
        />
        <circle
          cx="15"
          cy="14.5"
          r="1.9"
          fill="currentColor"
        />
      </template>
      <template v-else-if="kind === 'sidebar-toggle'">
        <path
          d="M13.5 5.5 7.5 12l6 6.5M18.5 5.5 12.5 12l6 6.5"
          stroke="currentColor"
          stroke-width="2.3"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
      </template>
      <template v-else>
        <!-- spark / emblem / pane-empty: Street-pop CRT head with lightning antenna -->
        <rect
          x="3"
          y="6.5"
          width="18"
          height="13.5"
          rx="3.5"
          stroke="currentColor"
          stroke-width="2.1"
        />
        <path
          d="M8.5 6.5 6 2.8M15.5 6.5 18 2.8"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
        />
        <circle
          cx="9"
          cy="13"
          r="2.2"
          fill="currentColor"
        />
        <circle
          cx="15"
          cy="13"
          r="2.2"
          fill="currentColor"
        />
      </template>
    </template>

    <!-- ====================================================================
         FRONTIER (RDR2) — 1899 sheriff star, compass rose & engraved bookplate
         ==================================================================== -->
    <template v-else-if="gamePalette === 'frontier'">
      <template v-if="kind === 'appearance'">
        <circle
          cx="12"
          cy="12"
          r="9.5"
          stroke="currentColor"
          stroke-width="1.6"
        />
        <polygon
          points="12,3.6 14.3,8.6 19.8,9.2 15.6,12.9 16.8,18.3 12,15.5 7.2,18.3 8.4,12.9 4.2,9.2 9.7,8.6"
          stroke="currentColor"
          stroke-width="1.6"
          stroke-linejoin="round"
        />
        <circle
          cx="12"
          cy="12"
          r="2"
          fill="currentColor"
        />
      </template>
      <template v-else-if="kind === 'terminal'">
        <!-- Antique camp lantern / telegraph crest -->
        <path
          d="M8 4h8M10 4V2.5h4V4M7 7.5h10l-1.2 10H8.2L7 7.5ZM6 17.5h12v2.5H6v-2.5Z"
          stroke="currentColor"
          stroke-width="1.7"
          stroke-linejoin="round"
        />
        <polygon
          points="12,9.5 14.2,12.5 12,15.5 9.8,12.5"
          fill="currentColor"
        />
      </template>
      <template v-else-if="kind === 'workspace'">
        <!-- Bookplate ledger shield with diamond seal -->
        <path
          d="M4 4h16v11.5L12 20.5 4 15.5V4Z"
          stroke="currentColor"
          stroke-width="1.8"
          stroke-linejoin="round"
        />
        <polygon
          points="12,7.5 15.5,11.5 12,15.5 8.5,11.5"
          fill="currentColor"
        />
      </template>
      <template v-else-if="kind === 'chats'">
        <!-- Wax-sealed frontier dispatch scroll -->
        <rect
          x="3"
          y="5"
          width="18"
          height="14"
          rx="1"
          stroke="currentColor"
          stroke-width="1.8"
        />
        <path
          d="M3.5 6 12 12.2 20.5 6"
          stroke="currentColor"
          stroke-width="1.6"
        />
        <polygon
          points="12,10.2 14.5,12.8 12,15.4 9.5,12.8"
          fill="currentColor"
        />
      </template>
      <template v-else-if="kind === 'search'">
        <circle
          cx="10.2"
          cy="10.2"
          r="6.2"
          stroke="currentColor"
          stroke-width="1.8"
        />
        <polygon
          points="10.2,6.5 12.2,10.2 10.2,13.9 8.2,10.2"
          fill="currentColor"
        />
        <path
          d="M15 15 20.8 20.8"
          stroke="currentColor"
          stroke-width="2.1"
          stroke-linecap="round"
        />
      </template>
      <template v-else-if="kind === 'archive'">
        <!-- Iron-bound stagecoach strongbox -->
        <rect
          x="3"
          y="6"
          width="18"
          height="13.5"
          rx="1"
          stroke="currentColor"
          stroke-width="1.8"
        />
        <path
          d="M3 10.5h18M8 6v13.5M16 6v13.5"
          stroke="currentColor"
          stroke-width="1.5"
        />
        <polygon
          points="12,8.5 14,10.5 12,12.5 10,10.5"
          fill="currentColor"
        />
      </template>
      <template v-else-if="kind === 'sidebar-toggle'">
        <path
          d="M14.5 5 7.5 12l7 7M7.5 12h11"
          stroke="currentColor"
          stroke-width="1.9"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
        <polygon
          points="18.5,9.5 21,12 18.5,14.5 16,12"
          fill="currentColor"
        />
      </template>
      <template v-else>
        <!-- spark / emblem / pane-empty: 8-point engraved compass rose -->
        <polygon
          points="12,2.2 14.4,9.6 21.8,12 14.4,14.4 12,21.8 9.6,14.4 2.2,12 9.6,9.6"
          stroke="currentColor"
          stroke-width="1.7"
          stroke-linejoin="round"
        />
        <polygon
          points="12,7.2 16.8,12 12,16.8 7.2,12"
          fill="currentColor"
        />
      </template>
    </template>

    <!-- ====================================================================
         LOS SANTOS (GTA V) — Tactical radar reticle, GPS arrow & HUD telemetry
         ==================================================================== -->
    <template v-else-if="gamePalette === 'los-santos'">
      <template v-if="kind === 'appearance'">
        <circle
          cx="12"
          cy="12"
          r="9"
          stroke="currentColor"
          stroke-width="1.9"
        />
        <polygon
          points="12,5.5 16.5,16.8 12,14.2 7.5,16.8"
          fill="currentColor"
        />
      </template>
      <template v-else-if="kind === 'terminal'">
        <rect
          x="2.5"
          y="4.5"
          width="19"
          height="15"
          rx="1"
          stroke="currentColor"
          stroke-width="1.9"
        />
        <path
          d="M6.5 9.5 10 12l-3.5 2.5M12.5 15.5h5"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="square"
        />
        <rect
          x="16.5"
          y="7.5"
          width="2.2"
          height="3.5"
          fill="currentColor"
        />
      </template>
      <template v-else-if="kind === 'workspace'">
        <rect
          x="2.5"
          y="4.5"
          width="19"
          height="15"
          rx="1"
          stroke="currentColor"
          stroke-width="1.9"
        />
        <path
          d="M9 4.5v15M2.5 11.5h19"
          stroke="currentColor"
          stroke-width="1.6"
        />
        <polygon
          points="15.5,7.5 18.8,15.8 15.5,14 12.2,15.8"
          fill="currentColor"
        />
      </template>
      <template v-else-if="kind === 'chats'">
        <path
          d="M3 4.5h18v11.5H9.5L4.5 20v-4H3V4.5Z"
          stroke="currentColor"
          stroke-width="1.9"
          stroke-linejoin="miter"
        />
        <rect
          x="7"
          y="10.5"
          width="2.2"
          height="3"
          fill="currentColor"
        />
        <rect
          x="10.9"
          y="8.5"
          width="2.2"
          height="5"
          fill="currentColor"
        />
        <rect
          x="14.8"
          y="6.8"
          width="2.2"
          height="6.7"
          fill="currentColor"
        />
      </template>
      <template v-else-if="kind === 'search'">
        <path
          d="M3 8V3h5M16 3h5v5M21 16v5h-5M8 21H3v-5"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="square"
        />
        <circle
          cx="12"
          cy="12"
          r="4.2"
          stroke="currentColor"
          stroke-width="1.8"
        />
        <rect
          x="10.8"
          y="10.8"
          width="2.4"
          height="2.4"
          fill="currentColor"
        />
      </template>
      <template v-else-if="kind === 'archive'">
        <rect
          x="3"
          y="4.5"
          width="18"
          height="15"
          rx="1"
          stroke="currentColor"
          stroke-width="1.9"
        />
        <path
          d="M7 4.5v15M7 9.5h14"
          stroke="currentColor"
          stroke-width="1.6"
        />
        <rect
          x="11"
          y="12.5"
          width="6"
          height="3.2"
          fill="currentColor"
        />
      </template>
      <template v-else-if="kind === 'sidebar-toggle'">
        <path
          d="M6 5v14M16 6l-6 6 6 6"
          stroke="currentColor"
          stroke-width="2.1"
          stroke-linecap="square"
          stroke-linejoin="miter"
        />
      </template>
      <template v-else>
        <!-- spark / emblem / pane-empty: West Coast HUD radar & GPS north arrow -->
        <polygon
          points="7,2.8 17,2.8 21.2,7 21.2,17 17,21.2 7,21.2 2.8,17 2.8,7"
          stroke="currentColor"
          stroke-width="1.9"
        />
        <polygon
          points="12,6 16.5,16.8 12,14.2 7.5,16.8"
          fill="currentColor"
        />
      </template>
    </template>
  </svg>
  <slot v-else />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useAppStore } from '@/stores/appStore'
import {
  isGamePalette,
  type GameThemePalette,
  type ThemePalette,
} from '@/features/app-shell/themePreference'

export type ThemeChromeIconKind =
  | 'appearance'
  | 'terminal'
  | 'workspace'
  | 'chats'
  | 'search'
  | 'archive'
  | 'sidebar-toggle'
  | 'spark'
  | 'pane-empty'
  | 'emblem'

const props = withDefaults(defineProps<{
  kind: ThemeChromeIconKind
  palette?: ThemePalette | string
  size?: number
  flipped?: boolean
}>(), {
  palette: undefined,
  size: 16,
  flipped: false,
})

const appStore = useAppStore()
const gamePalette = computed<GameThemePalette | null>(() => {
  const candidate = props.palette ?? appStore.themePalette
  return isGamePalette(candidate) ? candidate : null
})
</script>

<style scoped>
.theme-chrome-icon {
  flex: 0 0 auto;
  display: inline-block;
  vertical-align: middle;
  pointer-events: none;
  transition: transform var(--ch-motion-fast);
}

.theme-chrome-icon.flipped {
  transform: rotate(180deg);
}
</style>
