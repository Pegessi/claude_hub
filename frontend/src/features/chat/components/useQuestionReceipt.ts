import { computed, onScopeDispose, ref, watch, type Ref } from 'vue'

interface ReceiptQuestion {
  turn_id: string
  call_id: string
  state: string
  expires_at: string
}

/** A server-owned claim survives reload and another channel answering the card. */
export function useQuestionReceipt(base: Ref<string>, turnId: Ref<string | null>, callId: Ref<string | null>) {
  const state = ref('')
  const remoteExpired = ref(false)
  let controller: AbortController | null = null
  let timer: ReturnType<typeof setTimeout> | null = null
  let generation = 0
  const stop = () => {
    generation++
    controller?.abort()
    if (timer) clearTimeout(timer)
  }
  watch([base, turnId, callId], () => {
    stop()
    state.value = ''
    remoteExpired.value = false
    const epoch = generation
    const poll = async () => {
      controller = new AbortController()
      try {
        const response = await fetch(`${base.value}/questions`, { signal: controller.signal, credentials: 'same-origin' })
        if (response.ok) {
          const body = await response.json() as { questions?: ReceiptQuestion[] }
          const question = body.questions?.find(q => q.turn_id === turnId.value && q.call_id === callId.value)
          if (epoch === generation && question) {
            state.value = question.state
            remoteExpired.value = Date.parse(question.expires_at) <= Date.now()
          }
        }
      } catch { /* Optional projection failure must not disable web answers. */ }
      if (epoch === generation) timer = setTimeout(() => { void poll() }, 3000)
    }
    if (base.value && turnId.value && callId.value) void poll()
  }, { immediate: true })
  onScopeDispose(stop)
  const blocked = computed(() => ['claimed', 'resolved', 'stale', 'uncertain'].includes(state.value))
  const label = computed(() => ({
    claimed: '回答已接收，正在投递', resolved: '已回答', stale: '问题已失效', uncertain: '回答状态待核实',
  }[state.value] ?? (remoteExpired.value ? '飞书回答已过期；仍可在此回答' : '')))
  return { blocked, label }
}
