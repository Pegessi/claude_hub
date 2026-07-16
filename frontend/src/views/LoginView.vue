<template>
  <div class="login-page">
    <div class="login-container">
      <div class="login-header">
        <h1>Claude Hub</h1>
        <p>Web-based persistent terminal service</p>
      </div>

      <div class="login-content">
        <LoadingButton
          class="feishu-login-btn ch-btn ch-btn--primary ch-btn--lg"
          :loading="isLoggingIn"
          loading-label="Redirecting to Feishu"
          @click="handleLogin"
        >
          <span class="feishu-icon">📎</span>
          <span>使用飞书登录</span>
        </LoadingButton>
      </div>

      <div class="login-footer">
        <p>需要配置飞书应用才能使用认证功能</p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import LoadingButton from '@/components/LoadingButton.vue'
import { useAuthStore } from '@/stores/authStore'

const authStore = useAuthStore()
const isLoggingIn = ref(false)

function handleLogin() {
  if (isLoggingIn.value) return
  isLoggingIn.value = true
  window.location.href = authStore.getLoginUrl()
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--ch-color-app-bg);
  color: var(--ch-color-text);
  padding: 24px;
}

.login-container {
  background: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-xl);
  padding: 48px;
  box-shadow: var(--ch-shadow-dialog);
  text-align: center;
  max-width: 400px;
  width: 90%;
}

.login-header h1 {
  margin: 0 0 8px 0;
  font-size: 2rem;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--ch-color-text-strong);
}

.login-header p {
  margin: 0 0 40px 0;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-base);
}

.feishu-login-btn {
  gap: 12px;
  width: 100%;
  height: 44px;
  padding: 0 24px;
  border-radius: var(--ch-radius-lg);
  font-size: var(--ch-font-size-lg);
  font-weight: 600;
  transition: background var(--ch-motion-standard),
              border-color var(--ch-motion-standard),
              color var(--ch-motion-standard),
              box-shadow var(--ch-motion-standard),
              transform var(--ch-motion-standard);
}

.feishu-login-btn:hover:not(:disabled) {
  transform: translateY(-2px);
  box-shadow: 0 8px 20px var(--ch-color-accent-ring);
}

.feishu-login-btn:active:not(:disabled) {
  transform: translateY(0);
}

.feishu-login-btn:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring), 0 8px 20px var(--ch-color-accent-ring);
}

.feishu-icon {
  font-size: 18px;
}

.login-footer {
  margin-top: 32px;
  padding-top: 24px;
  border-top: 1px solid var(--ch-color-border-muted);
}

.login-footer p {
  margin: 0;
  color: var(--ch-color-text-soft);
  font-size: var(--ch-font-size-sm);
}
</style>
