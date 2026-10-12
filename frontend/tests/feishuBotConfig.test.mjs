import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source=readFileSync(new URL('../src/utils/feishuBotConfig.ts',import.meta.url),'utf8')
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ES2022,target:ts.ScriptTarget.ES2020}}).outputText
const api=await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)
const pool={pool_revision:0,bots:[],pool_editable:true,deprecated_env:[],focus_bot_id:null}
const response=(body,status=200)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}})

const cases=[
  ['loadFeishuBotPool',[], 'GET','/api/feishu/bot/bots',undefined,pool],
  ['createFeishuBot',[{name:'A',app_id:'id',app_secret:'s'}], 'POST','/api/feishu/bot/bots',{name:'A',app_id:'id',app_secret:'s'},pool],
  ['replaceFeishuBotSecrets',['bot/1',{app_secret:'s2',expected_revision:3}], 'PUT','/api/feishu/bot/bots/bot%2F1/secrets',{app_secret:'s2',expected_revision:3},pool],
  ['updateFeishuBot',['b',{name:'B',enabled:false,expected_revision:4}], 'PATCH','/api/feishu/bot/bots/b',{name:'B',enabled:false,expected_revision:4},pool],
  ['deleteFeishuBot',['b',5], 'DELETE','/api/feishu/bot/bots/b',{expected_revision:5},pool],
  ['startFeishuPairing',['b',{tab_id:'t',expected_revision:6}], 'POST','/api/feishu/bot/bots/b/pair/start',{tab_id:'t',expected_revision:6},{bot_id:'b',revision:7,code:'123456',expires_at:'2026-01-01T00:00:00Z'}],
  ['activateFeishuPairing',['b',{pairing_id:'p',confirm_word:'654321',expected_revision:7}], 'POST','/api/feishu/bot/bots/b/pair/activate',{pairing_id:'p',confirm_word:'654321',expected_revision:7},pool],
  ['disconnectFeishuPairing',['b',8], 'DELETE','/api/feishu/bot/bots/b/pairing',{expected_revision:8},pool],
]
for(const [name,args,method,url,body,result] of cases){
  test(`${name} uses the frozen pool contract`,async t=>{
    let request
    t.mock.method(globalThis,'fetch',async(u,o={})=>{request={u,o};return response(result,name==='createFeishuBot'||name==='startFeishuPairing'?201:200)})
    assert.deepEqual(await api[name](...args),result)
    assert.equal(request.u,url)
    assert.equal(request.o.credentials,'same-origin')
    assert.equal(request.o.method??'GET',method)
    if(body===undefined) assert.equal(request.o.body,undefined)
    else assert.deepEqual(JSON.parse(request.o.body),body)
    assert.equal(JSON.stringify(body??{}).includes('force'),false)
  })
}

test('known errors use fixed messages and unknown details never leak',async t=>{
  let body={detail:'pairing_confirmation_mismatch'}
  t.mock.method(globalThis,'fetch',async()=>response(body,409))
  await assert.rejects(()=>api.loadFeishuBotPool(),e=>e.code==='pairing_confirmation_mismatch'&&!e.message.includes('654321'))
  body={detail:'secret-app-secret-value'}
  await assert.rejects(()=>api.loadFeishuBotPool(),e=>e.code===null&&!e.message.includes('app-secret-value'))
})

test('removed single-Bot routes and force are absent',()=>{
  assert.doesNotMatch(source,/\/config(?:\/status)?|\/bind\/start|\/binding(?:['"`])/)
  assert.doesNotMatch(source,/\/pairings/)
  assert.doesNotMatch(source,/\bforce\b/)
})


for (const [status, code, fragment] of [
  [409, 'pool_capacity_reached', 'capacity has been reached'],
  [409, 'chat_tab_workspace_changed', 'This Chat changed workspace'],
  [409, 'bot_credentials_read_only', 'environment credentials'],
  [404, 'chat_tab_not_found', 'This Chat no longer exists'],
]) {
  test(`${code} keeps its specific recovery guidance`, async t => {
    t.mock.method(globalThis, 'fetch', async () => response({ detail: code }, status))
    await assert.rejects(api.loadFeishuBotPool(), error => (
      error.status === status && error.code === code && error.message.includes(fragment)
    ))
  })
}
