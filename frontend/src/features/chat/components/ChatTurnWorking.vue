<template>
  <!-- The live footer of the turn in flight. It owns its one-second clock so
       the memoized turn above it never re-renders just to tick. -->
  <div
    class="turn-working"
    role="status"
  >
    <span
      class="turn-working-orb"
      aria-hidden="true"
    />
    <span class="turn-working-label">Working</span>
    <time
      class="turn-working-elapsed"
      aria-hidden="true"
    >{{ elapsedLabel }}</time>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const props = defineProps<{ startedAt: string | null }>()

const mountedAt = Date.now()
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  timer = setInterval(() => { now.value = Date.now() }, 1000)
})

onBeforeUnmount(() => {
  if (timer !== null) clearInterval(timer)
})

/** ``0:07``, ``2:41``, ``1:02:13`` — a stopwatch, not a coarse duration. */
const elapsedLabel = computed(() => {
  const parsed = props.startedAt ? Date.parse(props.startedAt) : Number.NaN
  const start = Number.isFinite(parsed) ? Math.min(parsed, mountedAt) : mountedAt
  const total = Math.max(0, Math.floor((now.value - start) / 1000))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = String(total % 60).padStart(2, '0')
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, '0')}:${seconds}` : `${minutes}:${seconds}`
})
</script>

<style scoped>
.turn-working {
  display: inline-flex;
  align-items: center;
  align-self: flex-start;
  gap: 9px;
  min-height: 26px;
  padding: 2px 8px;
  color: var(--ch-color-text-subtle);
  font-size: 12.5px;
}

.turn-working-orb {
  position: relative;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--ch-color-accent);
  box-shadow: 0 0 0 0 var(--ch-color-accent-halo, var(--ch-color-accent-ring));
  animation: turn-working-pulse 1.6s ease-out infinite;
}

.turn-working-label {
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

.turn-working-elapsed {
  color: var(--ch-color-text-subtle);
  font-variant-numeric: tabular-nums;
  opacity: 0.8;
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
