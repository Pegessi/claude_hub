<template>
  <span class="feishu-identity-mark">
    <FeishuBrandMark class="feishu-identity-mark__brand" />
    <img
      v-if="showAvatar"
      :key="props.identityKey"
      class="feishu-identity-mark__avatar"
      :src="props.avatarUrl || undefined"
      alt=""
      aria-hidden="true"
      @error="useBrandFallback"
    >
  </span>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import FeishuBrandMark from '@/components/FeishuBrandMark.vue'

const props = defineProps<{
  avatarUrl: string | null
  identityKey: string
}>()

const failedIdentityKey = ref<string | null>(null)
const showAvatar = computed(() => (
  Boolean(props.avatarUrl) && failedIdentityKey.value !== props.identityKey
))

function useBrandFallback(): void {
  failedIdentityKey.value = props.identityKey
}
</script>

<style scoped>
.feishu-identity-mark {
  position: relative;
  display: inline-grid;
  place-items: center;
  overflow: hidden;
  color: currentColor;
}

.feishu-identity-mark__brand {
  width: 72%;
  height: 72%;
}

.feishu-identity-mark__avatar {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
}
</style>
