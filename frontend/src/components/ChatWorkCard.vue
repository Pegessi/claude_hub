<template>
  <article
    class="chat-work-card"
    :data-work-id="work.id"
    :data-status="work.status"
  >
    <button
      class="summary"
      type="button"
      :aria-expanded="expanded"
      :aria-controls="detailsId"
      @click="expanded=!expanded"
    >
      <span
        class="dot"
        aria-hidden="true"
      /><span class="title">{{ work.title }}</span><span>{{ statusLabel }}</span><span aria-hidden="true">{{ expanded?'▾':'▸' }}</span>
    </button>
    <p
      v-if="importantResult"
      class="notice"
    >
      {{ importantResult }}
    </p>
    <div
      v-if="expanded"
      :id="detailsId"
      class="details"
    >
      <p class="legacy-note">
        Legacy Chat work is read-only. Create and manage new work in Agent Workspace.
      </p>
      <button
        type="button"
        :disabled="!work.workspace_id"
        @click="emit('openWorkspace',work.workspace_id)"
      >
        Open in Agent Workspace
      </button>
      <div class="meta">
        <span>{{ work.kind==='monitor'?'Monitor':'Task' }} · {{ work.agent_type }}{{ work.model?` / ${work.model}`:'' }}</span><span>{{ work.status }}</span>
      </div>
      <section
        v-if="work.latest_result"
        class="result"
        aria-label="Latest legacy work result"
      >
        <strong>{{ resultLabel }}</strong><p>{{ work.latest_result.summary }}</p>
        <details v-if="work.latest_result.validation||work.latest_result.report_id||work.latest_result.task_id">
          <summary>Reported validation and source</summary><p v-if="work.latest_result.validation">
            {{ work.latest_result.validation }}
          </p><p v-if="work.latest_result.report_id">
            Report: {{ work.latest_result.report_id }}
          </p><p v-if="work.latest_result.task_id">
            Task: {{ work.latest_result.task_id }}
          </p>
        </details>
        <time :datetime="work.latest_result.created_at">{{ formatTime(work.latest_result.created_at) }}</time>
      </section>
      <p v-else>
        No result recorded.
      </p>
      <details v-if="work.executions.length">
        <summary>Historical executions ({{ work.executions.length }})</summary><ol>
          <li
            v-for="run in work.executions"
            :key="run.task_id"
          >
            <span>{{ run.status }}{{ run.outcome?` · ${run.outcome}`:'' }} · {{ formatTime(run.updated_at) }}</span><p v-if="run.summary">
              {{ run.summary }}
            </p><small>Task: {{ run.task_id }}</small>
          </li>
        </ol>
      </details>
      <div
        v-if="!terminal"
        class="controls"
      >
        <button
          type="button"
          :disabled="disabled"
          title="Stop future legacy work and request interruption of the current worker"
          @click="emit('update',{action:'stop'})"
        >
          Stop legacy work
        </button><span
          v-if="busy"
          role="status"
        >Stopping…</span>
      </div>
      <p v-if="work.status==='stopped'">
        Future legacy work is stopped. A running external operation may still finish.
      </p>
    </div>
  </article>
</template>
<script setup lang="ts">
import { computed,ref } from 'vue'
import type { ChatWork,ChatWorkUpdate } from '@/types/chatWork'
const props=defineProps<{work:ChatWork;busy:boolean;disabled:boolean}>();const emit=defineEmits<{update:[change:ChatWorkUpdate];openWorkspace:[workspaceId:string]}>();const expanded=ref(false)
const detailsId=computed(()=>`chat-work-details-${props.work.id}`);const terminal=computed(()=>['stopped','completed'].includes(props.work.status))
const labels:Record<ChatWork['status'],string>={running:'Running',waiting:'Waiting',paused:'Paused legacy record',stopped:'Stop requested',completed:'Completed',failed:'Failed',review:'Needs review'}
const statusLabel=computed(()=>labels[props.work.status]);const resultLabel=computed(()=>({progress:'Latest update',anomaly:'Attention needed',completed:'Result',decision:'Decision needed',failed:'Execution failed'})[props.work.latest_result?.kind??'progress'])
const importantResult=computed(()=>{const result=props.work.latest_result;return!expanded.value&&result&&result.kind!=='progress'?result.summary:null})
function formatTime(value:string){const date=new Date(value);return Number.isNaN(date.getTime())?value:date.toLocaleString()}
</script>
<style scoped>
.chat-work-card{border:1px solid var(--ch-color-border-muted);border-radius:8px}.summary{display:flex;width:100%;min-height:36px;align-items:center;gap:8px;padding:7px 10px;border:0;background:transparent;color:inherit;text-align:left}.dot{width:7px;height:7px;border-radius:50%;background:var(--ch-color-accent)}.title{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}.details{padding:4px 10px 10px;overflow-wrap:anywhere}.legacy-note,.meta,small,time{color:var(--ch-color-text-muted)}.meta,.controls{display:flex;flex-wrap:wrap;gap:8px}.result{margin:10px 0;padding:8px;border-left:2px solid var(--ch-color-border)}.notice{margin:0 10px 8px 25px;color:var(--ch-color-text-muted)}.controls{margin-top:10px}button:disabled{opacity:.5}@media(pointer:coarse){button{min-height:44px}}
</style>
