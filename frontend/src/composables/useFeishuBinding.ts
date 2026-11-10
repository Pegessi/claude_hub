import { computed, ref, watch, type Ref } from 'vue'
import { useFeishuBotPoolStore } from '@/stores/feishuBotPoolStore'
import {
  activateFeishuPairing, bindBotGroup, botBindings, botMode, disconnectFeishuPairing,
  FeishuBotRequestError, listBotChats, startFeishuPairing,
  type FeishuBotChat, type FeishuBotMode, type FeishuPairStartResponse,
} from '@/utils/feishuBotConfig'

export type FeishuBindingViewState='unbound'|'pending'|'claimed'|'bound-current'|'error'

export interface FeishuBindingDependencies {
  store?: ReturnType<typeof useFeishuBotPoolStore>
  now?: () => number
  setTimer?: (callback: () => void, delay: number) => ReturnType<typeof setTimeout>
  clearTimer?: (timer: ReturnType<typeof setTimeout>) => void
  startPairing?: typeof startFeishuPairing
  activatePairing?: typeof activateFeishuPairing
  disconnectPairing?: typeof disconnectFeishuPairing
  listChats?: typeof listBotChats
  bindGroup?: typeof bindBotGroup
}
export function useFeishuBinding(
  tabId: Ref<string>,
  workspaceId?: Ref<string | null>,
  dependencies: FeishuBindingDependencies = {},
) {
  const store = dependencies.store ?? useFeishuBotPoolStore()
  const now = dependencies.now ?? Date.now
  const setTimer = dependencies.setTimer ?? ((callback, delay) => setTimeout(callback, delay))
  const cancelTimer = dependencies.clearTimer ?? (timer => clearTimeout(timer))
  const clockMs = ref(now())
  const seenClaimId = ref<string | null>(null)
  const startPairing = dependencies.startPairing ?? startFeishuPairing
  const activatePairing = dependencies.activatePairing ?? activateFeishuPairing
  const disconnectPairing = dependencies.disconnectPairing ?? disconnectFeishuPairing
  const listChats = dependencies.listChats ?? listBotChats
  const requestGroupBind = dependencies.bindGroup ?? bindBotGroup
  const selectedBotId=ref<string|null>(null),pendingCode=ref<FeishuPairStartResponse|null>(null),confirmWord=ref('')
  const error=ref<string|null>(null),needsLogin=ref(false),isHydrated=ref(false),isLoading=ref(false),isMutating=ref(false)
  // Group binding is a second, independent path: its own read and its own error,
  // so a failed group list never disturbs the p2p pairing state machine.
  const groups=ref<FeishuBotChat[]>([]),groupsLoaded=ref(false),groupsLoading=ref(false),groupsError=ref<string|null>(null)
  let epoch=0,controller:AbortController|null=null,pollTimer:ReturnType<typeof setTimeout>|null=null,polling=false
  let groupEpoch=0,groupController:AbortController|null=null

  const currentBot=computed(()=>store.activeBotForTab(tabId.value))
  const binding=computed(()=>store.activeBindingForTab(tabId.value))
  const selectedBot=computed(()=>store.botById(selectedBotId.value))
  const activeBot=computed(()=>currentBot.value??selectedBot.value)
  const mode=computed<FeishuBotMode>(()=>botMode(activeBot.value))
  const bindings=computed(()=>botBindings(activeBot.value))
const matchingClaims = computed(() => {
  const bot = selectedBot.value
  if (!bot) return []
  return [...bot.my_claims]
    .filter(item => item.tab_id === tabId.value)
    .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))
})
const claim = computed(() =>
  matchingClaims.value.find(item => Date.parse(item.expires_at) > clockMs.value) ?? null,
)
  const bots=computed(()=>store.bots.map(bot=>{
    const active=botBindings(bot)
    const disabled=botMode(bot)==='single'
      ? active.length>0&&!active.some(item=>item.tab_id===tabId.value)
      : false
    return {bot,disabled}
  }))
  const viewState = computed<FeishuBindingViewState>(() => {
    if (needsLogin.value) return 'error'
    if (binding.value) return 'bound-current'
    if (claim.value) return 'claimed'
    if (pendingCode.value) return 'pending'
    if (error.value) return 'error'
    return 'unbound'
  })
  const statusText=computed(()=>({unbound:'Not connected',pending:'Waiting for Feishu',claimed:'Confirmation required','bound-current':`Connected with ${currentBot.value?.feishu_name||currentBot.value?.name||'Bot'}`,error:'Connection unavailable'}[viewState.value]))

  function clearPollTimer() {
  if (pollTimer !== null) cancelTimer(pollTimer)
  pollTimer = null
}
function clearPairingAttempt(message: string | null = null) {
  pendingCode.value = null
  confirmWord.value = ''
  seenClaimId.value = null
  clearPollTimer()
  if (message) error.value = message
}
/** Drop the cached group list: it belongs to one Bot in one Chat, never to the next. */
function resetGroups() {
  groupEpoch += 1
  groupController?.abort()
  groupController = null
  groups.value = []
  groupsLoaded.value = false
  groupsLoading.value = false
  groupsError.value = null
}
function schedulePoll() {
  clearPollTimer()
  if (!polling || isMutating.value || isLoading.value) return
  clockMs.value = now()
  if (binding.value) { clearPairingAttempt(); return }
  const activeClaim = claim.value
  if (activeClaim) seenClaimId.value = activeClaim.pairing_id
  if (seenClaimId.value && !activeClaim) {
    clearPairingAttempt('The claimed pairing expired. Generate a new code.')
    return
  }
  if (!activeClaim && pendingCode.value && Date.parse(pendingCode.value.expires_at) <= clockMs.value) {
    clearPairingAttempt('The pairing code expired. Generate a new code.')
    return
  }
  if (!pendingCode.value && !seenClaimId.value) return
  pollTimer = setTimer(() => {
    pollTimer = null
    void refresh()
  }, 3000)
}

  function setFailure(cause:unknown,fallback:string){
    const e=cause instanceof FeishuBotRequestError?cause:null
    error.value=e?.message??fallback;needsLogin.value=e?.status===401
  }
  async function reconcileAfterFailure(cause: unknown, signal?: AbortSignal) {
  const e = cause instanceof FeishuBotRequestError ? cause : null
  if (!e || e.status === 409 || e.status === 404 || e.status === 500) {
    await store.refresh(signal)
  }
}

  async function loadGroups(): Promise<boolean> {
  const bot = selectedBot.value ?? currentBot.value
  if (!bot || isMutating.value || groupsLoading.value || needsLogin.value) return false
  const requestEpoch = ++groupEpoch
  groupController?.abort()
  const request = new AbortController()
  groupController = request
  const requestBotId = bot.bot_id, requestTabId = tabId.value
  groupsLoading.value = true
  groupsError.value = null
  try {
    const value = await listChats(bot.bot_id, request.signal)
    if (requestEpoch !== groupEpoch || request.signal.aborted) return false
    // The reply describes the Bot and Chat that asked for it; a selection or
    // Chat change while it was in flight makes it useless, not merely stale.
    if ((selectedBotId.value ?? currentBot.value?.bot_id) !== requestBotId || tabId.value !== requestTabId) return false
    // The endpoint lists groups only, so every returned chat is bindable; the
    // server refuses a private chat id itself (``chat_not_group``).
    groups.value = [...value.chats]
    groupsLoaded.value = true
    return true
  } catch (cause) {
    if (requestEpoch !== groupEpoch || request.signal.aborted) return false
    const e = cause instanceof FeishuBotRequestError ? cause : null
    groupsError.value = e?.message ?? 'The Bot groups could not be loaded. Try again.'
    needsLogin.value = e?.status === 401
    return false
  } finally {
    if (requestEpoch === groupEpoch) {
      groupController = null
      groupsLoading.value = false
    }
  }
}
/** Load the group list once per Bot; an explicit reload stays the user's choice after a failure. */
function ensureGroups(): void {
  if (groupsLoaded.value || groupsLoading.value || groupsError.value) return
  if (!(selectedBot.value ?? currentBot.value) || needsLogin.value || isMutating.value) return
  void loadGroups()
}
  async function refresh(): Promise<boolean> {
  if (isMutating.value) return false
  clearPollTimer()
  const requestEpoch = ++epoch
  controller?.abort()
  const request = new AbortController()
  controller = request
  isLoading.value = true; error.value = null; needsLogin.value = false
  try {
    const value = await store.refresh(request.signal)
    if (requestEpoch !== epoch || request.signal.aborted) return false
    clockMs.value = now()
    isHydrated.value = store.pool !== null
    needsLogin.value = store.errorStatus === 401
    if (!value && store.error) error.value = store.error
    if (needsLogin.value) { clearPairingAttempt(); return false }
    if (!selectedBotId.value || !selectedBot.value) {
      const nextId = currentBot.value?.bot_id
        ?? store.bots.find(bot => bot.enabled && bot.configured && (botMode(bot) === 'per_channel' || botBindings(bot).length === 0))?.bot_id ?? null
      if (nextId !== selectedBotId.value) { clearPairingAttempt(); resetGroups() }
      selectedBotId.value = nextId
    }
    if (claim.value) seenClaimId.value = claim.value.pairing_id
    if (binding.value) clearPairingAttempt()
    return store.pool !== null && !store.error
  } finally {
    if (requestEpoch === epoch) {
      controller = null
      isLoading.value = false
      schedulePoll()
    }
  }
}
  function selectBot(botId: string) {
  if (isMutating.value) return
  const bot = store.botById(botId)
  const active = botBindings(bot)
  if (!bot || (botMode(bot) === 'single' && active.length > 0 && !active.some(item => item.tab_id === tabId.value))) return
  epoch += 1
  controller?.abort(); controller = null; isLoading.value = false
  selectedBotId.value = botId
  clearPairingAttempt()
  resetGroups()
  error.value = null
  clockMs.value = now()
  schedulePoll()
}
  async function generateCode(): Promise<boolean> {
  const bot = selectedBot.value
  if (!bot || !bot.enabled || !bot.configured || botMode(bot) !== 'single' || botBindings(bot).length > 0 || isMutating.value || needsLogin.value) return false
  const { requestEpoch, request } = beginMutation()
  try {
    const next = await startPairing(bot.bot_id, { tab_id: tabId.value, ...(workspaceId?.value ? { workspace_id: workspaceId.value } : {}), expected_revision: bot.revision }, request.signal)
    if (requestEpoch !== epoch || request.signal.aborted) return false
    pendingCode.value = next
    confirmWord.value = ''
    seenClaimId.value = null
    clockMs.value = now()
    polling = true
    return true
  } catch (cause) {
    if (requestEpoch !== epoch || request.signal.aborted) return false
    setFailure(cause, 'Failed to generate a pairing code.')
    await reconcileAfterFailure(cause, request.signal)
    return false
  } finally { finishMutation(requestEpoch) }
}
  async function activate(): Promise<boolean> {
  clockMs.value = now()
  const bot = selectedBot.value, item = claim.value, word = confirmWord.value.trim()
  if (!bot || !item || word.length !== 6 || isMutating.value || needsLogin.value) {
    if (!item) schedulePoll()
    return false
  }
  const { requestEpoch, request } = beginMutation()
  try {
    const next = await activatePairing(bot.bot_id, { pairing_id: item.pairing_id, confirm_word: word, expected_revision: bot.revision }, request.signal)
    if (requestEpoch !== epoch || request.signal.aborted) return false
    store.applyPool(next)
    clearPairingAttempt()
    return true
  } catch (cause) {
    if (requestEpoch !== epoch || request.signal.aborted) return false
    setFailure(cause, 'Failed to activate the pairing.')
    await reconcileAfterFailure(cause, request.signal)
    return false
  } finally { finishMutation(requestEpoch) }
}
  async function bindGroup(chatId: string): Promise<boolean> {
  const bot = selectedBot.value ?? currentBot.value
  if (!bot || !chatId || !bot.enabled || !bot.configured || isMutating.value || needsLogin.value) return false
  const isPerChannel = botMode(bot) === 'per_channel'
  const active = botBindings(bot)
  if (!isPerChannel && active.length > 0) return false
  if (isPerChannel && active.length >= 8) return false
  const { requestEpoch, request } = beginMutation()
  try {
    // The mutation response is the authoritative pool snapshot: applyPool adopts
    // it and reconciles when the pool moved on while this request was open.
    const input = isPerChannel
      ? { chat_id: chatId, expected_revision: bot.revision }
      : { chat_id: chatId, tab_id: tabId.value, expected_revision: bot.revision }
    const next = await requestGroupBind(bot.bot_id, input, request.signal)
    if (requestEpoch !== epoch || request.signal.aborted) return false
    store.applyPool(next)
    groupsError.value = null
    clearPairingAttempt()
    return true
  } catch (cause) {
    if (requestEpoch !== epoch || request.signal.aborted) return false
    setFailure(cause, 'Failed to bind this group.')
    await reconcileAfterFailure(cause, request.signal)
    return false
  } finally { finishMutation(requestEpoch) }
}
  async function disconnect(): Promise<boolean> {
  const bot = currentBot.value
  if (!bot || isMutating.value || needsLogin.value) return false
  const { requestEpoch, request } = beginMutation()
  try {
    const next = await disconnectPairing(bot.bot_id, bot.revision, request.signal)
    if (requestEpoch !== epoch || request.signal.aborted) return false
    store.applyPool(next)
    selectedBotId.value = null
    clearPairingAttempt()
    return true
  } catch (cause) {
    if (requestEpoch !== epoch || request.signal.aborted) return false
    setFailure(cause, 'Failed to disconnect Feishu.')
    await reconcileAfterFailure(cause, request.signal)
    return false
  } finally { finishMutation(requestEpoch) }
}
  async function resume() {
  polling = true
  if (isLoading.value || isMutating.value) return
  await refresh()
}
function pause() {
  polling = false
  clearPollTimer()
  epoch += 1
  controller?.abort(); controller = null
  resetGroups()
  isLoading.value = false; isMutating.value = false
  confirmWord.value = ''
}
watch([tabId, () => workspaceId?.value ?? null], () => {
  const active = polling
  pause()
  selectedBotId.value = null
  clearPairingAttempt()
  error.value = null; needsLogin.value = false; isHydrated.value = false
  if (active) void resume()
}, { flush: 'sync' })
watch(() => claim.value?.pairing_id ?? null, () => {
  confirmWord.value = ''
}, { flush: 'sync' })

function beginMutation() {
  clearPollTimer()
  const requestEpoch = ++epoch
  controller?.abort()
  const request = new AbortController()
  controller = request
  isLoading.value = false
  isMutating.value = true
  error.value = null; needsLogin.value = false
  return { requestEpoch, request }
}
function finishMutation(requestEpoch: number) {
  if (requestEpoch !== epoch) return
  controller = null
  isMutating.value = false
  schedulePoll()
}

  return{bots,binding,bindings,mode,currentBot,selectedBotId,selectedBot,pendingCode,claim,confirmWord,error,needsLogin,isHydrated,isLoading,isMutating,viewState,statusText,groups,groupsLoaded,groupsLoading,groupsError,refresh,selectBot,generateCode,activate,bindGroup,loadGroups,ensureGroups,disconnect,resume,pause}
}
