<template>
  <span
    class="agent-avatar"
    :class="`agent-avatar--${kind}`"
    :data-size="size"
    :title="title"
    :aria-label="title"
  >
    <!-- claude: Anthropic-style asterisk mark on amber background -->
    <svg
      v-if="kind === 'claude'"
      viewBox="0 0 24 24"
      class="agent-avatar__glyph"
      aria-hidden="true"
    >
      <path
        d="M5.4 18 10.1 6h2.6l4.7 12h-2.5l-1-2.7h-4.9L8 18H5.4Zm4.3-4.7h3.5L11.45 8.6h-.05l-1.7 4.7Z"
        fill="currentColor"
      />
    </svg>

    <!-- codex: angular OpenAI-style hex glyph -->
    <svg
      v-else-if="kind === 'codex'"
      viewBox="0 0 24 24"
      class="agent-avatar__glyph"
      aria-hidden="true"
    >
      <path
        d="M12 3 4.5 7.2v9.6L12 21l7.5-4.2V7.2L12 3Zm0 2.4 5.4 3v7.2L12 18.6 6.6 15.6V8.4L12 5.4Zm-3.6 4.2v4.8L12 16.2l3.6-2.1V9.6L12 7.5 8.4 9.6Z"
        fill="currentColor"
      />
    </svg>

    <!-- cursor: pointer arrow mark -->
    <svg
      v-else-if="kind === 'cursor'"
      viewBox="0 0 24 24"
      class="agent-avatar__glyph"
      aria-hidden="true"
    >
      <path
        d="M5 4v15.5l4.4-3.7 2.8 5.4 2.4-1.2-2.7-5.3H18L5 4Z"
        fill="currentColor"
      />
    </svg>

    <!-- terminal: prompt glyph -->
    <svg
      v-else
      viewBox="0 0 24 24"
      class="agent-avatar__glyph"
      aria-hidden="true"
    >
      <path
        d="m6 8 4 4-4 4 1.4 1.4L13 12 7.4 6.6 6 8Zm7 8h6v2h-6v-2Z"
        fill="currentColor"
      />
    </svg>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { AgentType } from '@/types'

type AvatarKind = AgentType

const props = withDefaults(defineProps<{
  agentType?: AgentType | null
  size?: 'sm' | 'md'
}>(), {
  agentType: 'terminal',
  size: 'md',
})

const kind = computed<AvatarKind>(() => {
  const raw = props.agentType
  if (raw === 'claude' || raw === 'codex' || raw === 'cursor' || raw === 'terminal') return raw
  return 'terminal'
})

const title = computed(() => `${kind.value} agent`)
</script>

<style scoped>
.agent-avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  border-radius: 8px;
  width: 28px;
  height: 28px;
  color: #fff;
  background: #4b4b4b;
  box-shadow: inset 0 0 0 1px var(--ch-color-border-muted, rgba(255, 255, 255, 0.1));
  overflow: hidden;
}

.agent-avatar[data-size='sm'] {
  width: 22px;
  height: 22px;
  border-radius: 6px;
}

.agent-avatar__glyph {
  width: 70%;
  height: 70%;
}

.agent-avatar--claude {
  background: linear-gradient(135deg, #d97757 0%, #c5532f 100%);
  color: #fff;
}

.agent-avatar--codex {
  background: linear-gradient(135deg, #1f2937 0%, #0f172a 100%);
  color: #10a37f;
}

.agent-avatar--cursor {
  background: linear-gradient(135deg, #f5f5f5 0%, #d4d4d4 100%);
  color: #111;
}

.agent-avatar--terminal {
  background: linear-gradient(135deg, #2a2a2a 0%, #1a1a1a 100%);
  color: #7ee787;
}
</style>
