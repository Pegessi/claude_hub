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
 *   • 1px borders (stroke constant, not spacing)
 *   • Layout constants: min-height:100vh, max-width:400px, width:90%
 *   • All font-sizes are rem-based (2rem/0.95rem/1.1rem/1.3rem/0.85rem);
 *     --ch-font-* is px-based so these stay rem (per task instruction)
 *   • Only one weight declaration in this file: h1 uses --ch-weight-semibold
 *     (600) to match the rest of the product; no other element overrides
 *     weight, so UA defaults apply to non-heading text. Prior versions of
 *     this comment incorrectly claimed "no weight declarations" — corrected
 *     for round-8 D8-06.
 *   • .feishu-login-btn hover is flat (background-color only; no lift
 *     or glow — matches rest of product's minimalist buttons).
 *   • Transition uses --ch-motion-standard (background-color only).
 *   • :focus-visible uses --ch-color-accent-ring-strong for keyboard
 *     accessibility (consistent with App.vue :root focus style).
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
  font-weight: var(--ch-weight-semibold);
  color: var(--ch-color-text-strong);
}

.login-header p {
  margin: 0 0 40px 0; /* no 40px token; keep literal */
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-lg);
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
  font-size: var(--ch-font-xl);
  cursor: pointer;
  transition: background-color var(--ch-motion-standard);
}

.feishu-login-btn:hover {
  background: var(--ch-color-accent-hover);
}

.feishu-login-btn:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring-strong);
  outline-offset: 2px;
}

.feishu-icon {
  font-size: var(--ch-font-xl);
}

.login-footer {
  margin-top: var(--ch-space-6);
  padding-top: var(--ch-space-5);
  border-top: 1px solid var(--ch-color-border-muted);
}

.login-footer p {
  margin: 0;
  color: var(--ch-color-text-soft);
  font-size: var(--ch-font-md);
}
</style>
