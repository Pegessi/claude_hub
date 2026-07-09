<template>
  <div class="login-page">
    <div class="login-container">
      <div class="login-header">
        <h1>Claude Hub</h1>
        <p>Web-based persistent terminal service</p>
      </div>

      <div class="login-content">
        <LoadingButton
          class="feishu-login-btn"
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
/*
 * Login page — Feishu SSO landing card.
 *
 * Styling consumes the global design-token scale (--ch-space-*,
 * --ch-radius-*) where values match exactly. Per the conservative
 * mapping rule for this pass, off-scale hardcoded px are LEFT LITERAL
 * with a short comment when no token resolves to the same pixel value:
 *   • .login-container padding:48px (generous intentional card padding;
 *     max space token is space-6=32px — composing tokens is not allowed)
 *   • .login-container border-radius:8px (--ch-radius-md=7px, lg=10px;
 *     neither equals 8; per exact-match rule)
 *   • .login-header p margin-bottom:40px (no 40px token)
 *   • .feishu-login-btn transition:all 0.2s ease (--ch-motion-standard
 *     is 180ms; 200ms is not an exact match)
 *   • Hover box-shadow 0 8px 20px (shadow-spread parameter, not a
 *     spacing token; no matching --ch-shadow-* for accent-color glow)
 *   • translateY(-2px) hover lift (affordance constant)
 *   • 1px borders (stroke constant, not spacing)
 *   • Layout constants: min-height:100vh, max-width:400px, width:90%
 *   • All font-sizes are rem-based (2rem/0.95rem/1.1rem/1.3rem/0.85rem);
 *     --ch-font-* is px-based so these stay rem (per task instruction)
 *   • No font-weight declarations exist in this file; weight tokens
 *     are a no-op.
 */

.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--ch-color-app-bg);
  color: var(--ch-color-text);
  padding: var(--ch-space-5);
}

.login-container {
  background: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border);
  border-radius: 8px; /* no exact token; md=7px lg=10px — keep literal */
  padding: 48px; /* generous card padding; max space token is 32px, composing disallowed */
  box-shadow: var(--ch-shadow-dialog);
  text-align: center;
  max-width: 400px;
  width: 90%;
}

.login-header h1 {
  margin: 0 0 var(--ch-space-2) 0;
  font-size: 2rem;
  color: var(--ch-color-text-strong);
}

.login-header p {
  margin: 0 0 40px 0; /* no 40px token; keep literal */
  color: var(--ch-color-text-muted);
  font-size: 0.95rem;
}

.feishu-login-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--ch-space-3);
  width: 100%;
  padding: var(--ch-space-4) var(--ch-space-5);
  background: var(--ch-color-accent-strong);
  color: var(--ch-color-text-inverse);
  border: none;
  border-radius: var(--ch-radius-lg);
  font-size: 1.1rem;
  cursor: pointer;
  transition: all 0.2s ease; /* --ch-motion-standard=180ms ≠ 200ms; keep literal */
}

.feishu-login-btn:hover {
  background: var(--ch-color-accent-hover);
  transform: translateY(-2px);
  box-shadow: 0 8px 20px var(--ch-color-accent-ring);
}

.feishu-login-btn:active {
  transform: translateY(0);
}

.feishu-icon {
  font-size: 1.3rem;
}

.login-footer {
  margin-top: var(--ch-space-6);
  padding-top: var(--ch-space-5);
  border-top: 1px solid var(--ch-color-border-muted);
}

.login-footer p {
  margin: 0;
  color: var(--ch-color-text-soft);
  font-size: 0.85rem;
}
</style>
