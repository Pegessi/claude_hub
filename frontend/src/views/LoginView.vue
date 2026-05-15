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
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
}

.login-container {
  background: white;
  border-radius: 16px;
  padding: 48px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
  text-align: center;
  max-width: 400px;
  width: 90%;
}

.login-header h1 {
  margin: 0 0 8px 0;
  font-size: 2rem;
  color: #333;
}

.login-header p {
  margin: 0 0 40px 0;
  color: #666;
  font-size: 0.95rem;
}

.feishu-login-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  width: 100%;
  padding: 16px 24px;
  background: #2f81ff;
  color: white;
  border: none;
  border-radius: 10px;
  font-size: 1.1rem;
  cursor: pointer;
  transition: all 0.2s ease;
}

.feishu-login-btn:hover {
  background: #1a6de8;
  transform: translateY(-2px);
  box-shadow: 0 8px 20px rgba(47, 129, 255, 0.4);
}

.feishu-login-btn:active {
  transform: translateY(0);
}

.feishu-icon {
  font-size: 1.3rem;
}

.login-footer {
  margin-top: 32px;
  padding-top: 24px;
  border-top: 1px solid #eee;
}

.login-footer p {
  margin: 0;
  color: #999;
  font-size: 0.85rem;
}
</style>
