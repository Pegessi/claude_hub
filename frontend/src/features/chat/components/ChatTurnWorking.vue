<template>
  <!-- The live footer of the turn in flight. It owns its one-second clock so
       the memoized turn above it never re-renders just to tick.

       Long turns used to show a lone "Working 21:15" under an otherwise empty
       screen. Two lines fix that without adding animation: what the agent is
       doing right now, and a quiet count of what it has already done. Waiting
       states stop the pulse, so a turn blocked on the user or on a child
       thread never reads as progress. -->
  <div
    class="turn-working"
    :class="{ 'turn-working--waiting': step.waiting }"
    role="status"
  >
    <div class="turn-working-line">
      <span
        class="turn-working-orb"
        aria-hidden="true"
      />
      <!-- A blocked turn drops the "Working" word instead of dimming it: its
           own sentence is the whole line, and nothing on it claims progress. -->
      <span
        v-if="!step.waiting"
        class="turn-working-label"
      >Working</span>
      <span
        v-if="step.label"
        class="turn-working-step"
      >{{ step.waiting ? step.label : `· ${step.label}` }}</span>
      <time
        class="turn-working-elapsed"
        aria-hidden="true"
      >{{ elapsedLabel }}</time>
    </div>
    <div
      v-if="countsLabel"
      class="turn-working-counts"
    >
      <span
        class="turn-working-counts-text"
        :class="{ 'turn-working-counts-text--failed': counts.failed > 0 }"
      >{{ countsLabel }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { formatStopwatch, parseTimestampMs } from '@/utils/duration'
import { requireChatOwner, chatTimelineKey } from '../owners'
import { countWorkLog, currentStep, emptyWorkLogCounts, workLogCountLabel } from '../worklog/workLog'
import type { TimelineTurn } from '../timeline/types'

const props = defineProps<{ startedAt: string | null }>()

// This footer only renders for the turn in flight, which is the newest turn of
// the pane — reading it from the owner keeps ChatTurn.vue from having to pass
// the whole part list down just for a label.
const { turns } = requireChatOwner(chatTimelineKey)

const liveTurn = computed<TimelineTurn | null>(() => turns.value[turns.value.length - 1] ?? null)

const step = computed(() => liveTurn.value ? currentStep(liveTurn.value) : { label: '', waiting: false })

const counts = computed(() => liveTurn.value ? countWorkLog(liveTurn.value) : emptyWorkLogCounts())

const countsLabel = computed(() => workLogCountLabel(counts.value))

const mountedAt = Date.now()
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  timer = setInterval(() => { now.value = Date.now() }, 1000)
})

onBeforeUnmount(() => {
  if (timer !== null) clearInterval(timer)
})

// A future server timestamp (client clock behind) must not show negative time.
const elapsedLabel = computed(() => {
  const parsed = parseTimestampMs(props.startedAt)
  const start = parsed === null ? mountedAt : Math.min(parsed, mountedAt)
  return formatStopwatch(now.value - start)
})
</script>

<style scoped>
.turn-working {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  align-self: flex-start;
  gap: 1px;
  max-width: 100%;
  min-height: 26px;
  padding: 2px 8px;
  color: var(--ch-color-text-subtle);
  font-size: 12.5px;
}

.turn-working-line {
  display: flex;
  align-items: baseline;
  gap: 7px;
  max-width: 100%;
  min-width: 0;
  min-height: 22px;
}

.turn-working-orb {
  position: relative;
  flex: 0 0 auto;
  align-self: center;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--ch-color-accent);
  box-shadow: 0 0 0 0 var(--ch-color-accent-halo, var(--ch-color-accent-ring));
  animation: turn-working-pulse 1.6s ease-out infinite;
}

.turn-working-label {
  flex: 0 0 auto;
  background: linear-gradient(
    90deg,
    var(--ch-color-text-subtle) 0%,
    var(--ch-color-text-strong, var(--ch-color-text)) 50%,
    var(--ch-color-text-subtle) 100%
  );
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  font-weight: 500;
  animation: turn-working-shimmer 1.8s linear infinite;
}

/* The current step is the payload of this line, so it keeps the readable
   text colour and truncates rather than pushing the timer off the row. */
.turn-working-step {
  min-width: 0;
  overflow: hidden;
  color: var(--ch-color-text-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.turn-working-elapsed {
  flex: 0 0 auto;
  color: var(--ch-color-text-subtle);
  font-variant-numeric: tabular-nums;
  opacity: 0.8;
}

.turn-working-counts {
  max-width: 100%;
  padding-left: 15px;
  font-size: 11.5px;
  line-height: 1.45;
}

.turn-working-counts-text {
  color: var(--ch-color-text-subtle);
  opacity: 0.85;
}

.turn-working-counts-text--failed {
  color: var(--ch-color-danger-strong, #f85149);
  opacity: 1;
}

/* Blocked, not busy: no pulse and a hollow dot. */
.turn-working--waiting .turn-working-orb {
  background: transparent;
  box-shadow: inset 0 0 0 1px var(--ch-color-text-subtle);
  animation: none;
}

@keyframes turn-working-pulse {
  0% { box-shadow: 0 0 0 0 var(--ch-color-accent-halo, var(--ch-color-accent-ring)); }
  70% { box-shadow: 0 0 0 7px transparent; }
  100% { box-shadow: 0 0 0 0 transparent; }
}

@keyframes turn-working-shimmer {
  from { background-position: 100% 0; }
  to { background-position: -100% 0; }
}

@media (prefers-reduced-motion: reduce) {
  .turn-working-orb,
  .turn-working-label {
    animation: none;
  }

  .turn-working-label {
    color: var(--ch-color-text-muted);
    background: none;
  }
}
</style>
