import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { User, AuthCheckResponse } from '@/types'

const API_BASE = '/api'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null)
  const isLoading = ref(false)
  const authRequired = ref(false)

  const isAuthenticated = computed(() => user.value !== null)
  const authEnabled = computed(() => authRequired.value)

  async function checkAuth(): Promise<void> {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/auth/check`, {
        credentials: 'include',
      })
      const data: AuthCheckResponse = await response.json()
      authRequired.value = data.auth_required
      user.value = data.user
    } catch (error) {
      console.error('Failed to check auth:', error)
      user.value = null
    } finally {
      isLoading.value = false
    }
  }

  async function fetchUser(): Promise<void> {
    try {
      const response = await fetch(`${API_BASE}/auth/me`, {
        credentials: 'include',
      })
      if (response.ok) {
        user.value = await response.json()
      } else {
        user.value = null
      }
    } catch (error) {
      console.error('Failed to fetch user:', error)
      user.value = null
    }
  }

  function getLoginUrl(): string {
    return `${API_BASE}/auth/login`
  }

  async function logout(): Promise<void> {
    try {
      await fetch(`${API_BASE}/auth/logout`, {
        method: 'POST',
        credentials: 'include',
      })
    } catch (error) {
      console.error('Failed to logout:', error)
    } finally {
      user.value = null
    }
  }

  function getSessionIdFromCookie(): string | null {
    if (typeof document === 'undefined') return null
    const name = 'claude_hub_session='
    const decodedCookie = decodeURIComponent(document.cookie)
    const ca = decodedCookie.split(';')
    for (let i = 0; i < ca.length; i++) {
      let c = ca[i]
      while (c.charAt(0) === ' ') {
        c = c.substring(1)
      }
      if (c.indexOf(name) === 0) {
        return c.substring(name.length, c.length)
      }
    }
    return null
  }

  return {
    user,
    isLoading,
    authRequired,
    authEnabled,
    isAuthenticated,
    checkAuth,
    fetchUser,
    getLoginUrl,
    logout,
    getSessionIdFromCookie,
  }
})
