<template>
  <button
    v-bind="$attrs"
    :type="type"
    class="loading-button"
    :class="{
      'loading-button--loading': loading,
      'loading-button--hide-content': loading && hideContentWhileLoading,
    }"
    :disabled="disabled || loading"
    :aria-busy="loading ? 'true' : undefined"
  >
    <span
      v-if="loading"
      class="loading-button__spinner"
      aria-hidden="true"
    />
    <span class="loading-button__content">
      <slot />
    </span>
    <span
      v-if="loading && loadingLabel"
      class="loading-button__sr-only"
    >
      {{ loadingLabel }}
    </span>
  </button>
</template>

<script setup lang="ts">
defineOptions({ inheritAttrs: false })

type ButtonType = 'button' | 'submit' | 'reset'

withDefaults(defineProps<{
  loading?: boolean
  disabled?: boolean
  type?: ButtonType
  loadingLabel?: string
  hideContentWhileLoading?: boolean
}>(), {
  loading: false,
  disabled: false,
  type: 'button',
  loadingLabel: 'Working',
  hideContentWhileLoading: false,
})
</script>

<style scoped>
.loading-button {
  position: relative;
}

.loading-button:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring-strong);
  outline-offset: 2px;
  border-radius: inherit;
}

.loading-button__spinner {
  width: 1em;
  height: 1em;
  display: inline-block;
  flex: 0 0 auto;
  margin-right: 0.45em;
  vertical-align: -0.125em;
  border: 2px solid currentColor;
  border-right-color: transparent;
  border-radius: 999px;
  animation: loading-button-spin 700ms linear infinite;
}

.loading-button__content {
  display: contents;
}

.loading-button--hide-content .loading-button__spinner {
  margin-right: 0;
}

.loading-button--hide-content .loading-button__content {
  display: none;
}

.loading-button__sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

@keyframes loading-button-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
