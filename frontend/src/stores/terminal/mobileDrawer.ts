import { ref } from 'vue'

export function createMobileDrawer(selectTab: (tabId: string) => void) {
  const mobileDrawerOpen = ref(false)

  function openMobileDrawer() {
    mobileDrawerOpen.value = true
  }

  function closeMobileDrawer() {
    mobileDrawerOpen.value = false
  }

  function toggleMobileDrawer() {
    mobileDrawerOpen.value = !mobileDrawerOpen.value
  }

  function selectMobileTab(tabId: string) {
    selectTab(tabId)
    closeMobileDrawer()
  }

  return {
    mobileDrawerOpen,
    openMobileDrawer,
    closeMobileDrawer,
    toggleMobileDrawer,
    selectMobileTab,
  }
}
