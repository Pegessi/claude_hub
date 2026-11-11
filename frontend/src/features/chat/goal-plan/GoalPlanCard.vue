<template>
  <section
    class="goal-plan-card"
    :data-mode="mode"
    :aria-labelledby="headingId"
  >
    <header class="goal-plan-header">
      <span
        :id="headingId"
        class="goal-plan-eyebrow"
      >Execution plan</span>
      <p
        v-if="plan?.summary"
        class="goal-plan-summary"
      >
        {{ plan.summary }}
      </p>
      <p
        v-else-if="mode === 'preparing'"
        class="goal-plan-summary"
      >
        Drafting the plan. No work starts until you approve it.
      </p>
    </header>

    <ol
      v-if="plan && plan.steps.length"
      class="goal-plan-steps"
    >
      <li
        v-for="(step, index) in plan.steps"
        :key="index"
      >
        <strong>{{ step.title }}</strong>
        <span
          v-if="step.detail"
          class="goal-plan-step-detail"
        >{{ step.detail }}</span>
        <span
          v-if="step.acceptance"
          class="goal-plan-step-acceptance"
        >Verified by: {{ step.acceptance }}</span>
      </li>
    </ol>

    <div
      v-if="plan"
      class="goal-plan-budget"
    >
      <strong>Execution budget</strong>
      <p class="goal-plan-budget-summary">
        {{ budgetSummary }}
      </p>
      <ul
        v-if="plan.budget.helpers.length"
        class="goal-plan-budget-helpers"
      >
        <li
          v-for="(helper, index) in plan.budget.helpers"
          :key="index"
        >
          <span class="goal-plan-helper-role">{{ helper.role || 'Helper' }}</span>
          <span class="goal-plan-helper-model">{{ helper.provider }}/{{ helper.model }}</span>
        </li>
      </ul>
    </div>

    <p
      v-if="!plan && mode === 'review'"
      class="goal-plan-missing"
      role="status"
    >
      The Goal is waiting for your input, but no plan was proposed to approve.
      Ask for a revised plan instead of starting work.
    </p>

    <p
      v-if="warning"
      class="goal-plan-warning"
      role="status"
    >
      {{ warning }}
    </p>

    <div
      v-if="mode === 'review'"
      class="goal-plan-actions"
    >
      <button
        type="button"
        class="goal-plan-approve"
        :disabled="Boolean(approveDisabledReason)"
        :title="approveDisabledReason || 'Start the Goal with this plan'"
        @click="emit('approve')"
      >
        Approve &amp; start
      </button>
      <button
        type="button"
        class="goal-plan-revise"
        :disabled="Boolean(reviseDisabledReason)"
        :title="reviseDisabledReason || 'Ask the agent to revise this plan before starting'"
        @click="emit('revise')"
      >
        Revise
      </button>
    </div>
    <p
      v-else-if="mode === 'continuing'"
      class="goal-plan-note"
    >
      The Goal is continuing with this plan during its current turn.
    </p>
    <p
      v-else-if="mode === 'answered'"
      class="goal-plan-note"
    >
      The Goal continued after this plan.
    </p>
    <p
      v-else-if="mode === 'waiting'"
      class="goal-plan-note"
    >
      The Goal moved on without approving this plan.
    </p>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { ChatGoalPlan } from '@/types'
import { goalPlanBudgetSummary, type GoalPlanCardMode } from './planCard'

const props = defineProps<{
  plan: ChatGoalPlan | null
  mode: GoalPlanCardMode
  /** Unique per turn: several cards can be on screen at once. */
  headingId: string
  warning?: string | null
  approveDisabledReason?: string | null
  /** Revise stays available when Approve is refused for a missing plan. */
  reviseDisabledReason?: string | null
}>()
const emit = defineEmits<{ approve: []; revise: [] }>()
const budgetSummary = computed(() => goalPlanBudgetSummary(props.plan))
</script>

<style scoped>
.goal-plan-card {
  display: grid;
  gap: 10px;
  margin: 8px 0 4px;
  padding: 12px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
}
.goal-plan-header { display: grid; gap: 4px; }
.goal-plan-eyebrow {
  font-size: var(--ch-font-size-xs);
  font-weight: var(--ch-weight-medium, 500);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--ch-color-text-subtle);
}
.goal-plan-summary { margin: 0; font-size: var(--ch-font-size-base); color: var(--ch-color-text); }
.goal-plan-steps {
  margin: 0;
  padding-left: 20px;
  display: grid;
  gap: 6px;
  list-style: decimal;
  font-size: var(--ch-font-size-sm);
  color: var(--ch-color-text-muted);
}
.goal-plan-steps ::marker { color: var(--ch-color-text-subtle); }
/* ``display: grid`` on an ``li`` removes its marker, so the number must stay
   on a list item and each line is a block inside it. */
.goal-plan-steps li { line-height: 1.5; }
.goal-plan-steps strong { display: block; color: var(--ch-color-text); font-weight: var(--ch-weight-medium, 500); }
.goal-plan-step-detail { display: block; }
.goal-plan-step-acceptance { display: block; color: var(--ch-color-text-subtle); }
.goal-plan-budget {
  display: grid;
  gap: 4px;
  padding: 8px 10px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-sunken);
  font-size: var(--ch-font-size-sm);
}
.goal-plan-budget strong { color: var(--ch-color-text); font-weight: var(--ch-weight-medium, 500); }
.goal-plan-budget-summary { margin: 0; color: var(--ch-color-text-muted); }
.goal-plan-budget-helpers {
  margin: 0;
  padding-left: 16px;
  display: grid;
  gap: 2px;
  list-style: none;
  color: var(--ch-color-text-muted);
}
.goal-plan-budget-helpers li { display: flex; gap: 8px; }
.goal-plan-helper-role { color: var(--ch-color-text); }
.goal-plan-helper-model { font-family: var(--ch-font-mono); color: var(--ch-color-text-soft); }
.goal-plan-missing { margin: 0; font-size: var(--ch-font-size-sm); line-height: 1.5; color: var(--ch-color-text-muted); }
.goal-plan-warning { margin: 0; font-size: var(--ch-font-size-sm); color: var(--ch-color-warning); }
.goal-plan-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.goal-plan-actions button {
  padding: 7px 12px;
  color: var(--ch-color-text);
  background: var(--ch-color-surface-control);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  font: inherit;
  cursor: pointer;
}
.goal-plan-actions button:hover:not(:disabled) { background: var(--ch-color-surface-control-hover); }
.goal-plan-actions .goal-plan-approve {
  color: var(--ch-color-on-accent, #fff);
  background: var(--ch-color-accent-strong);
  border-color: var(--ch-color-accent-strong);
}
.goal-plan-actions .goal-plan-approve:hover:not(:disabled) { background: var(--ch-color-accent-fill-hover); border-color: var(--ch-color-accent-fill-hover); }
.goal-plan-actions button:disabled { cursor: default; opacity: 0.55; }
.goal-plan-actions button:focus-visible { outline: 2px solid var(--ch-color-accent-ring); outline-offset: 2px; }
.goal-plan-note { margin: 0; font-size: var(--ch-font-size-sm); color: var(--ch-color-text-subtle); }
@media (max-width: 640px), (pointer: coarse) {
  .goal-plan-actions button { min-height: 44px; }
}
</style>
