<template>
  <div class="empty-state">
    <div
      v-if="$slots.icon"
      class="empty-state__icon"
      aria-hidden="true"
    >
      <slot name="icon" />
    </div>
    <p
      v-if="title || $slots.title"
      class="empty-state__title"
    >
      <slot name="title">
        {{ title }}
      </slot>
    </p>
    <p
      v-if="description || $slots.description"
      class="empty-state__description"
    >
      <slot name="description">
        {{ description }}
      </slot>
    </p>
    <div
      v-if="$slots.action"
      class="empty-state__action"
    >
      <slot name="action" />
    </div>
  </div>
</template>

<script setup lang="ts">
/*
 * EmptyState — the canonical "nothing here yet" primitive.
 *
 * Restrained, centered, token-driven. Use it wherever a list/panel/editor has
 * no content to show, so the empty affordance stays consistent app-wide.
 *
 * Slots:
 *   icon        — optional leading glyph/illustration (rendered muted, aria-hidden)
 *   title       — headline; falls back to the `title` prop
 *   description — optional muted helper text; falls back to the `description` prop
 *   action      — optional trailing control (e.g. a "Create" button)
 *
 * Every design-token reference carries a literal fallback (var(--token, <value>))
 * so the component renders correctly whether or not the host app defines the
 * --ch-* scale.
 *
 * Example:
 *   <EmptyState title="No presets yet" description="Create one to get started.">
 *     <template #icon>📦</template>
 *     <template #action><button @click="create">New preset</button></template>
 *   </EmptyState>
 */
withDefaults(defineProps<{
  title?: string
  description?: string
}>(), {
  title: '',
  description: '',
})
</script>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  gap: var(--ch-space-2, 8px);
  padding: var(--ch-space-6, 32px) var(--ch-space-4, 16px);
  color: var(--ch-color-text-soft, #888);
}

.empty-state__icon {
  font-size: 32px; /* large empty-state glyph; sits above the UI body type scale */
  line-height: 1;
  margin-bottom: var(--ch-space-1, 4px);
  color: var(--ch-color-text-soft, #888);
}

.empty-state__title {
  margin: 0;
  font-size: var(--ch-font-md, 13px);
  font-weight: var(--ch-weight-medium, 500);
  color: var(--ch-color-text-muted, #a1a1aa);
}

.empty-state__description {
  margin: 0;
  max-width: 42ch;
  font-size: var(--ch-font-xs, 11px);
  line-height: var(--ch-leading-normal, 1.5);
  color: var(--ch-color-text-soft, #888);
}

.empty-state__action {
  margin-top: var(--ch-space-3, 12px);
}
</style>
