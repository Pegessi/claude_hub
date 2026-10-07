import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { FeishuBotRequestError, loadFeishuBotPool, type FeishuActiveBinding, type FeishuBotPoolResponse, type FeishuBotSummary } from '@/utils/feishuBotConfig'

export const useFeishuBotPoolStore = defineStore('feishuBotPool', () => {
  const pool=ref<FeishuBotPoolResponse|null>(null),loading=ref(false),error=ref<string|null>(null),errorStatus=ref<number|null>(null)
  let refreshEpoch=0,lastAdoptedPoolRevision=-1,recovery:Promise<void>|null=null
  const bots=computed(()=>pool.value?.bots??[]),poolEditable=computed(()=>pool.value?.pool_editable??false),deprecatedEnv=computed(()=>pool.value?.deprecated_env??[])

  function adopt(value:FeishuBotPoolResponse):void{
    pool.value=value
    lastAdoptedPoolRevision=value.pool_revision
    error.value=null
    errorStatus.value=null
  }
  async function recoverStale(baseline: number): Promise<void> {
  if (recovery) return recovery
  recovery = (async () => {
    let observed = baseline
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        const value = await loadFeishuBotPool()
        if (
          value.pool_revision >= lastAdoptedPoolRevision ||
          lastAdoptedPoolRevision === observed
        ) {
          adopt(value)
          return
        }
      } catch (cause) {
        // A response adopted meanwhile also makes this read's error stale.
        if (lastAdoptedPoolRevision === observed) {
          error.value = cause instanceof FeishuBotRequestError
            ? cause.message : 'The Feishu Bot pool could not be reconciled.'
          errorStatus.value = cause instanceof FeishuBotRequestError ? cause.status : null
          return
        }
      }
      observed = lastAdoptedPoolRevision
    }
    error.value = 'The Bot pool changed during reconciliation. Reload before another action.'
    errorStatus.value = null
  })().finally(() => { recovery = null })
  return recovery
}
  function consider(value:FeishuBotPoolResponse,recover=true):boolean{
    if(value.pool_revision>=lastAdoptedPoolRevision){adopt(value);return true}
    if(recover)void recoverStale(lastAdoptedPoolRevision)
    return false
  }
  // Call for every mutation response. It invalidates an older GET before considering the full-pool snapshot.
  function applyPool(value:FeishuBotPoolResponse):boolean{
    refreshEpoch+=1
    loading.value=false
    return consider(value,true)
  }
  function botById(id:string|null|undefined):FeishuBotSummary|null{return id?bots.value.find(bot=>bot.bot_id===id)??null:null}
  function activeBotForTab(tabId:string):FeishuBotSummary|null{return bots.value.find(bot=>bot.binding?.tab_id===tabId)??null}
  function activeBindingForTab(tabId:string):FeishuActiveBinding|null{return activeBotForTab(tabId)?.binding??null}
  async function refresh(signal?:AbortSignal):Promise<FeishuBotPoolResponse|null>{
    const epoch=++refreshEpoch;loading.value=true;error.value=null;errorStatus.value=null
    try{
      const value=await loadFeishuBotPool(signal)
      if(epoch!==refreshEpoch||signal?.aborted)return null
      consider(value,true)
      return value
    }catch(cause){
      if(epoch!==refreshEpoch||signal?.aborted||(cause instanceof DOMException&&cause.name==='AbortError'))return null
      error.value=cause instanceof FeishuBotRequestError?cause.message:'The Feishu Bot pool could not be loaded.'
      errorStatus.value=cause instanceof FeishuBotRequestError?cause.status:null
      return null
    }finally{if(epoch===refreshEpoch)loading.value=false}
  }
  function clearError(){error.value=null;errorStatus.value=null}
  function invalidateRefresh(){refreshEpoch+=1;loading.value=false}
  return{pool,bots,poolEditable,deprecatedEnv,loading,error,errorStatus,applyPool,botById,activeBotForTab,activeBindingForTab,refresh,clearError,invalidateRefresh}
})
