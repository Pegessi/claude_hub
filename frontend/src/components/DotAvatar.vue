<template>
  <span
    class="dot-avatar"
    :style="{ '--dot-avatar-size': `${size}px` }"
    aria-hidden="true"
  >
    <img
      v-if="imageUrl && !imageFailed"
      class="dot-avatar__image"
      :src="imageUrl"
      alt=""
      @error="imageFailed = true"
    >
    <span
      v-else-if="identity.avatar"
      class="dot-avatar__text"
    >{{ identity.avatar }}</span>
    <span
      v-else
      class="dot-avatar__mark"
    />
  </span>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { dotFileUrl, type DotIdentity } from '@/utils/dotApi'

const props = withDefaults(defineProps<{ identity: DotIdentity, size?: number }>(), { size: 20 })

const imageFailed = ref(false)
const imageUrl = computed(() => (props.identity.avatar_image ? dotFileUrl(props.identity.avatar_image) : ''))
watch(imageUrl, () => { imageFailed.value = false })
</script>

<style scoped>
.dot-avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  width: var(--dot-avatar-size);
  height: var(--dot-avatar-size);
  border-radius: 50%;
  overflow: hidden;
}

.dot-avatar__image {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.dot-avatar__text {
  font-size: calc(var(--dot-avatar-size) * 0.8);
  line-height: 1;
}

.dot-avatar__mark {
  width: 40%;
  height: 40%;
  border-radius: 50%;
  background: var(--ch-color-accent);
  box-shadow: 0 0 0 calc(var(--dot-avatar-size) * 0.15) var(--ch-color-accent-ring);
}
</style>
