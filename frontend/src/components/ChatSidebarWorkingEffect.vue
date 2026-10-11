<template>
  <!--
    Working-session light for one Chat sidebar row: a Maso-style light pulse
    that orbits the rounded border, plus a field of stars inside the row that
    each ignite as the pulse passes their column. Every animation shares one
    orbit duration and starts on mount, so the stars trail the pulse in
    lockstep.
  -->
  <span
    class="working-effect"
    aria-hidden="true"
  >
    <!-- rx follows --ch-radius-md (6px) minus half of the 1px border. -->
    <svg
      class="working-effect__beam"
      focusable="false"
    >
      <g class="working-effect__orbit">
        <rect
          class="working-effect__bloom"
          width="100%"
          height="100%"
          rx="5.5"
          pathLength="100"
          :stroke-dasharray="BLOOM_DASHARRAY"
        />
        <rect
          v-for="(dasharray, index) in BEAM_LAYERS"
          :key="index"
          class="working-effect__layer"
          width="100%"
          height="100%"
          rx="5.5"
          pathLength="100"
          :stroke-dasharray="dasharray"
        />
      </g>
    </svg>
    <span class="working-effect__sky">
      <span class="working-effect__pool" />
      <span
        v-for="(star, index) in stars"
        :key="index"
        :class="`working-effect__${star.kind}`"
        :style="{
          left: `${star.x}%`,
          top: `${star.y}%`,
          '--star-size': `${star.size}px`,
          '--star-pass': star.pass,
        }"
      />
    </span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  WORKING_BEAM_TAIL,
  createWorkingBeamLayers,
  createWorkingStarfield,
} from '@/utils/workingSessionEffect'

const props = defineProps<{
  /** Stable per-row seed (the tab id) so each row gets its own star field. */
  seed: string
}>()

const BEAM_LAYERS = createWorkingBeamLayers()
/** A soft glow under the brightest part of the pulse, ending at the lead. */
const BLOOM_DASHARRAY = `0 ${WORKING_BEAM_TAIL - 6} 8 ${100 - WORKING_BEAM_TAIL - 2}`

const stars = computed(() => createWorkingStarfield(props.seed))
</script>

<style scoped>
.working-effect {
  --working-orbit: 3.6s;
  position: absolute;
  inset: -1px;
  z-index: 0;
  border-radius: inherit;
  pointer-events: none;
}

/* The SVG box sits on the centerline of the row's 1px border, so the pulse
   travels exactly along the border. */
.working-effect__beam {
  position: absolute;
  inset: 0.5px;
  width: calc(100% - 1px);
  height: calc(100% - 1px);
  overflow: visible;
}

.working-effect__beam rect {
  fill: none;
}

/* Dash arrays come from createWorkingBeamLayers: every layer straddles the
   pulse peak at WORKING_BEAM_TAIL, so starting the shared offset there puts
   the peak on the orbit phase, and one lap moves every layer together. */
.working-effect__orbit {
  stroke-dashoffset: 18;
  animation: working-effect-orbit var(--working-orbit) linear infinite;
}

.working-effect__layer {
  stroke: var(--ch-color-session-glow-strong);
  stroke-width: 1.2px;
  opacity: 0.09;
}

.working-effect__bloom {
  stroke: var(--ch-color-session-star-halo);
  stroke-width: 3px;
  opacity: 0.35;
  filter: blur(2px);
}

@keyframes working-effect-orbit {
  to { stroke-dashoffset: -82; }
}

.working-effect__sky {
  position: absolute;
  inset: 1px;
  overflow: hidden;
  border-radius: calc(var(--ch-radius-md) - 1px);
  contain: paint;
}

/* A soft pool of light that follows the beam head around the inside of the
   row. The row-sized box carries the motion (percent translations equal the
   row size) and its pseudo-element is the glow centered on the box corner.
   Offsets mirror WORKING_BEAM_EDGE_SHARE in utils/workingSessionEffect. */
.working-effect__pool {
  position: absolute;
  inset: 0;
  animation: working-effect-pool var(--working-orbit) linear infinite;
}

.working-effect__pool::before {
  content: '';
  position: absolute;
  left: -60px;
  top: -32px;
  width: 120px;
  height: 64px;
  background: radial-gradient(
    closest-side,
    color-mix(in srgb, var(--ch-color-session-star-halo) 16%, transparent),
    transparent
  );
}

@keyframes working-effect-pool {
  0% { transform: translate(0, 0); }
  42.5% { transform: translate(100%, 0); }
  50% { transform: translate(100%, 100%); }
  92.5% { transform: translate(0, 100%); }
  100% { transform: translate(0, 0); }
}

/* Negative delays keep every star on the orbit's phase: the keyframe peak at
   3% lands when the beam passes the star's column. */
.working-effect__dust,
.working-effect__glint {
  position: absolute;
  width: var(--star-size);
  height: var(--star-size);
  margin: calc(var(--star-size) / -2) 0 0 calc(var(--star-size) / -2);
  border-radius: 50%;
  animation-duration: var(--working-orbit);
  animation-timing-function: linear;
  animation-iteration-count: infinite;
  animation-delay: calc(var(--working-orbit) * (var(--star-pass) - 1.03));
}

.working-effect__dust {
  background: var(--ch-color-session-star);
  box-shadow: 0 0 3px 0.5px var(--ch-color-session-star-halo);
  opacity: 0.12;
  animation-name: working-effect-dust;
}

@keyframes working-effect-dust {
  0% { opacity: 0.12; transform: scale(0.7); }
  3% { opacity: 0.8; transform: scale(1.3); }
  12% { opacity: 0.48; transform: scale(1); }
  32% { opacity: 0.14; transform: scale(0.75); }
  60% { opacity: 0.14; transform: scale(0.75); }
  65% { opacity: 0.36; transform: scale(0.95); }
  72% { opacity: 0.12; transform: scale(0.7); }
  100% { opacity: 0.12; transform: scale(0.7); }
}

.working-effect__glint {
  border-radius: 0;
  background:
    radial-gradient(circle, var(--ch-color-session-star) 0 1px, var(--ch-color-session-star-halo) 2px, transparent 3.5px),
    linear-gradient(90deg, transparent, var(--ch-color-session-star), transparent) center / 100% 1px no-repeat,
    linear-gradient(transparent, var(--ch-color-session-star), transparent) center / 1px 100% no-repeat;
  opacity: 0;
  animation-name: working-effect-glint;
}

@keyframes working-effect-glint {
  0% { opacity: 0; transform: scale(0.2) rotate(0deg); }
  4% { opacity: 0.7; transform: scale(1) rotate(45deg); }
  18% { opacity: 0; transform: scale(0.3) rotate(90deg); }
  100% { opacity: 0; transform: scale(0.2) rotate(90deg); }
}

@media (prefers-reduced-motion: reduce) {
  .working-effect {
    display: none;
  }
}
</style>
