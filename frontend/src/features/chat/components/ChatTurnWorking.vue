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
           own sentence is the whole line, and nothing on it claims progress.
           A turn that has not reported anything yet does the same, because
           "Working · Waiting for the first response" contradicts itself. -->
      <span
        v-if="!step.waiting && !step.bare"
        class="turn-working-label"
      >Working</span>
      <span
        v-if="step.label"
        class="turn-working-step"
      >{{ step.waiting || step.bare ? step.label : `· ${step.label}` }}</span>
      <time
        class="turn-working-elapsed"
        aria-hidden="true"
      >{{ elapsedLabel }}</time>
    </div>
    <div
      v-if="countsLabel"
      class="turn-working-counts"
    >
      <span class="turn-working-counts-text">
        <template
          v-if="countParts.prefix"
        >{{ countParts.prefix }}</template>
        <span
          v-if="countParts.failed"
          class="turn-working-counts-failed"
        >{{ countParts.prefix ? ` · ${countParts.failed}` : countParts.failed }}</span>
        <template
          v-if="countParts.suffix"
        >{{ `${countParts.prefix || countParts.failed ? ' · ' : ''}${countParts.suffix}` }}</template>
      </span>
    </div>
    <!-- Nothing has come back yet: say why that can be, once it is worth saying.
         A bare clock in an empty pane reads like a stall. -->
    <div
      v-if="hint"
      class="turn-working-hint"
    >
      {{ hint }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref } from 'vue'
import { formatStopwatch, parseTimestampMs } from '@/utils/duration'
import { chatIdentityKey, requireChatOwner, chatTimelineKey } from '../owners'
import {
  countWorkLog,
  currentStep,
  emptyWorkLogCounts,
  firstResponseHint,
  workLogCountLabel,
  workLogCountParts,
} from '../worklog/workLog'

const props = defineProps<{ startedAt: string | null }>()

// This footer only renders for the turn in flight, which is the newest turn of
// the pane — reading it from the owner keeps ChatTurn.vue from having to pass
// the whole part list down just for a label.
const { turns, turnEventCount } = requireChatOwner(chatTimelineKey)

// The Hub's own view of this tab is what makes a silent turn legible: "running,
// nothing written yet" and "offline, nothing can arrive" look identical in a
// transcript otherwise. Optional so the footer still projects a turn in fixtures.
const identity = inject(chatIdentityKey, null)

/** The turn in flight is the newest turn of the pane. Read it straight from
 *  ``turns`` in each projection, never through an intermediate computed that
 *  returns the turn object: ``IncrementalTimelineReducer`` mutates turns in
 *  place and returns a fresh array, so the turn reference never changes and a
 *  computed handing it back would never invalidate — the footer would keep
 *  the first label of a 20-minute turn forever. The array reference does
 *  change per batch, so depending on it is what keeps this live. */
const step = computed(() => {
  const turn = turns.value[turns.value.length - 1]
  return turn ? currentStep(turn, turnEventCount(turn) !== null) : { label: '', waiting: false }
})

const counts = computed(() => {
  const turn = turns.value[turns.value.length - 1]
  return turn ? countWorkLog(turn) : emptyWorkLogCounts()
})

/** The event count is the volume of work even when nothing has been reduced
 *  into tools yet, so it rides on the counts line and grows with the stream. */
const eventCount = computed(() => {
  const turn = turns.value[turns.value.length - 1]
  return turn ? turnEventCount(turn) : null
})

/** ``N events so far``: the fold above it inventories the turn, this line
 *  reports how fast it is filling, so the wording has to say it is still moving. */
const eventsPhrase = computed(() => {
  if (eventCount.value === null) return ''
  return `${eventCount.value} event${eventCount.value === 1 ? '' : 's'} so far`
})

const countsLabel = computed(() => {
  const label = workLogCountLabel(counts.value)
  if (eventsPhrase.value === '') return label
  return label ? `${label} · ${eventsPhrase.value}` : eventsPhrase.value
})

/** The failure phrase is its own span: one failed call inside a long turn must
 *  not repaint the whole summary, so only that phrase carries the warning tone. */
const countParts = computed(() => {
  const parts = workLogCountParts(counts.value)
  const tail = [parts.suffix, eventsPhrase.value].filter(Boolean).join(' · ')
  return { ...parts, suffix: tail }
})



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
const elapsedMs = computed(() => {
  const parsed = parseTimestampMs(props.startedAt)
  const start = parsed === null ? mountedAt : Math.min(parsed, mountedAt)
  return now.value - start
})

const elapsedLabel = computed(() => formatStopwatch(elapsedMs.value))

/** Only the window before the first event gets an explanation: everywhere else
 *  the step itself says what is happening. */
const hint = computed(() => {
  if (!step.value.bare) return ''
  return firstResponseHint({
    elapsedMs: elapsedMs.value,
    connectionState: identity?.connectionState?.value ?? 'live',
    runtime: identity?.runtimeStatus?.value ?? null,
  })
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

/* Centre the dot on the text line, not on its baseline: at 12.5px the baseline
   offset put the light 4-5px low and it read as misaligned. The fixed line-height
   keeps the dot centred as the label word is swapped between states. */
.turn-working-line {
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: 100%;
  min-width: 0;
  min-height: 22px;
  line-height: 20px;
}

.turn-working-orb {
  position: relative;
  flex: 0 0 auto;
  align-self: center;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--ch-color-accent);
  box-shadow: 0 0 0 0 var(--ch-color-accent-halo, var(--ch-color-accent-ring));
  animation: turn-working-pulse 1.8s ease-out infinite;
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

/* The reason behind a silent start; one short sentence, never a second status. */
.turn-working-hint {
  max-width: 100%;
  padding-left: 15px;
  color: var(--ch-color-text-subtle);
  font-size: 11.5px;
  line-height: 1.45;
  opacity: 0.8;
}

.turn-working-counts-text {
  color: var(--ch-color-text-subtle);
  opacity: 0.85;
}

/* A partial failure is not a stalled turn: the phrase that names it takes the
   amber warning tone, the rest of the line stays as quiet as before. */
.turn-working-counts-failed {
  color: var(--ch-color-warning-strong, var(--ch-color-warning, #f59e0b));
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
  70% { box-shadow: 0 0 0 4px transparent; }
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
