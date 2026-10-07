<template>
  <section
    v-if="work.length||error"
    class="chat-work-panel"
    aria-label="Legacy background work"
  >
    <div class="header">
      <button
        type="button"
        :aria-expanded="expanded"
        :aria-controls="listId"
        @click="expanded=!expanded"
      >
        <span aria-hidden="true">{{ expanded?'▾':'▸' }}</span><strong>Legacy background work</strong><span>{{ work.length }}</span><span
          v-if="attentionCount"
          class="attention"
        >{{ attentionCount }} need attention</span>
      </button>
      <div class="header-actions">
        <button
          type="button"
          :disabled="refreshing||Boolean(busyId)"
          @click="refresh"
        >
          {{ refreshing?'Refreshing…':'Refresh' }}
        </button>
      </div>
    </div>
    <p class="migration-note">
      Existing Chat work remains available for history and explicit stopping. Create and manage new Tasks in Agent Workspace.
    </p>
    <p
      v-if="error"
      class="error"
      role="alert"
    >
      {{ stale?'Status may be out of date. ':'' }}{{ error }}
    </p>
    <div
      v-if="expanded"
      :id="listId"
      class="list"
    >
      <ChatWorkCard
        v-for="item in work"
        :key="item.id"
        :work="item"
        :busy="busyId===item.id"
        :disabled="Boolean(busyId)||stale"
        @update="change=>update(item.id,change)"
        @open-workspace="openWorkspace"
      />
    </div>
  </section>
</template>
<script setup lang="ts">
import { computed,onActivated,onDeactivated,onMounted,onUnmounted,ref,toRef,watch } from 'vue'
import ChatWorkCard from '@/components/ChatWorkCard.vue'
import { useChatWork } from '@/composables/useChatWork'
import { useAppStore } from '@/stores/appStore'
import { useWorkspaceStore } from '@/stores/workspaceStore'
import { useTerminalStore } from '@/stores/terminalStore'
import { createWorkResultTracker } from '@/utils/chatWorkNotifications'
const props=defineProps<{tabId:string;refreshKey?:number}>();const appStore=useAppStore();const terminalStore=useTerminalStore()
const workspaceStore=useWorkspaceStore()
function openWorkspace(workspaceId:string){if(!workspaceId)return;workspaceStore.setActiveWorkspace(workspaceId);appStore.setMode('workspace')}
let storage: Storage | null = null
try {
  storage = localStorage
} catch {
  // The result tracker can use memory when browser storage is unavailable.
}
const observed = createWorkResultTracker(storage)
const {work,error,refreshing,busyId,stale,refresh,update,start,stop,dispose}=useChatWork(toRef(props,'tabId'));const expanded=ref(true);const listId=computed(()=>`chat-work-list-${props.tabId}`)
const attentionCount=computed(()=>work.value.filter(item=>item.status==='failed'||item.status==='review'||['anomaly','decision'].includes(item.latest_result?.kind??'')).length);let active=false
watch(work,items=>{if(!active||appStore.mode!=='terminal'||document.visibilityState==='hidden')return;const updates=observed(items);if(!updates.length)return;const first=updates[0];terminalStore.pushNotification({type:updates.some(item=>item.latest_result?.kind!=='completed')?'warning':'success',message:updates.length===1?`${first.title}: ${first.latest_result?.summary??'New result'}`.slice(0,320):`${updates.length} legacy work results are ready.`,autoDismissMs:8000})})
function reconcile(){if(active&&appStore.mode==='terminal'&&document.visibilityState!=='hidden')void start();else stop()}
function activate(){active=true;reconcile()}function deactivate(){active=false;stop()}
onMounted(()=>{document.addEventListener('visibilitychange',reconcile);activate()});onActivated(activate);onDeactivated(deactivate);onUnmounted(()=>{document.removeEventListener('visibilitychange',reconcile);dispose()})
watch(()=>props.refreshKey,()=>{if(active)void refresh()});watch(()=>props.tabId,()=>{expanded.value=true});watch(()=>appStore.mode,reconcile)
</script>
<style scoped>
.chat-work-panel{flex:0 0 auto;border-top:1px solid var(--ch-color-border-muted);padding:6px 24px 9px;font-size:12px}.header,.header-actions{display:flex;justify-content:space-between;align-items:center;gap:8px}.header button{min-height:30px;border:0;background:transparent;color:var(--ch-color-text-muted)}.attention{color:var(--ch-color-warning)}.migration-note{margin:5px 0;color:var(--ch-color-text-muted)}.list{display:grid;gap:6px;max-height:min(36dvh,380px);overflow:auto}.error{color:var(--ch-color-danger)}@media(max-width:640px){.chat-work-panel{padding:5px 10px}.attention{display:none}}@media(pointer:coarse){button{min-height:44px}}
</style>
