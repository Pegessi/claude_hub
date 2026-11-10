import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  activeBinding, bot, changeSelect, chatList, clickNode, config,
  findAllByTestId, findByTestId, groupChat, harness,
  mountBindingPanel, mountSettings, pool, response, settleUi, textOf,
} from './feishuBotUiTestHarness.mjs'

const pane = readFileSync(new URL('../src/components/TerminalPane.vue', import.meta.url), 'utf8')
const structuredPane = readFileSync(new URL('../src/features/chat/StructuredPane.vue', import.meta.url), 'utf8')
const userMessage = readFileSync(new URL('../src/features/chat/components/ChatUserMessage.vue', import.meta.url), 'utf8')
const timeline = readFileSync(new URL('../src/features/chat/components/ChatTimeline.vue', import.meta.url), 'utf8')
const actions = readFileSync(new URL('../src/features/chat/controllers/useChatActions.ts', import.meta.url), 'utf8')
const panel = readFileSync(new URL('../src/components/FeishuBindingPanel.vue', import.meta.url), 'utf8')
const binding = readFileSync(new URL('../src/composables/useFeishuBinding.ts', import.meta.url), 'utf8')
const identity = readFileSync(new URL('../src/utils/feishuIdentity.ts', import.meta.url), 'utf8')
const identityMark = readFileSync(new URL('../src/components/FeishuIdentityMark.vue', import.meta.url), 'utf8')
const brandMark = readFileSync(new URL('../src/components/FeishuBrandMark.vue', import.meta.url), 'utf8')

test('local direct Chat surface mounts one Feishu binding panel beside its session name', () => {
  assert.match(pane, /class="pane-chrome"[\s\S]*class="pane-session-name"[\s\S]*<FeishuBindingPanel/)
  assert.match(pane, /v-if="isChatSession && !isManagedChat"[\s\S]*:tab-id="pane\.tabId"/)
  assert.match(pane, /import FeishuBindingPanel from '@\/components\/FeishuBindingPanel\.vue'/)
  assert.doesNotMatch(structuredPane, /<FeishuBindingPanel/)
  assert.doesNotMatch(structuredPane, /import FeishuBindingPanel/)
})

test('message source is rendered from each durable turn instead of binding state or turn id', () => {
  assert.match(userMessage, /:data-message-origin="turn\.origin"/)
  assert.match(userMessage, /v-if="turn\.origin === 'feishu'"/)
  assert.match(userMessage, /\{\{ messageSourceLabel\(turn\) \}\}/)
  assert.doesNotMatch(userMessage, /turn\.turnId.*(?:startsWith|includes).*feishu/)
})

test('pool binding retains accessible status and explicit pairing controls', () => {
  for (const marker of [':aria-label="`Feishu connection settings · ${statusText}`"', 'data-testid="feishu-binding-trigger"', 'data-testid="feishu-binding-status"', ':data-state="viewState"', 'Generate pairing code', 'Activate pairing', 'Confirm disconnect']) {
    assert.ok(panel.includes(marker), marker)
  }
  assert.ok(panel.includes('href="/api/auth/login"'))
  assert.ok(panel.includes('v-model="confirmWord"'))
  assert.ok(panel.includes('status-dot'))
  assert.doesNotMatch(binding, /owner_open_id/)
})

test('the Feishu status trigger matches the pane chrome control geometry', () => {
  assert.match(panel, /\.feishu-binding\s*\{[\s\S]*position:\s*relative;[\s\S]*--pane-chrome-control-size, 24px/)
  assert.match(panel, /border-radius: 999px/)
  assert.match(panel, /background: var\(--ch-color-surface-raised\)/)
  assert.match(panel, /box-shadow: 0 1px 4px var\(--ch-shadow-color-soft\)/)
  assert.match(panel, /\.feishu-binding \{[\s\S]*height: var\(--pane-chrome-control-size, 24px\)/)
})

test('the Feishu entry reuses the monochrome bird mark and scopes Bot avatars to Bot identity', () => {
  assert.match(panel, /<FeishuBrandMark[\s\S]{0,160}class="trigger-icon"/)
  assert.match(panel, /<FeishuBrandMark[\s\S]{0,160}class="panel-mark"/)
  assert.equal((panel.match(/<FeishuBrandMark/g) || []).length, 2)
  assert.doesNotMatch(panel, /<svg[\s\S]{0,160}class="trigger-icon"/)
  assert.match(brandMark, /data-feishu-brand-mark="bird"/)
  assert.equal((brandMark.match(/<path/g) || []).length, 3)
  assert.match(brandMark, /fill:\s*none/)
  assert.match(brandMark, /stroke:\s*currentColor/)
  assert.match(panel, /data-testid="feishu-binding-identity"/)
  assert.match(panel, /viewState.value !== 'bound-current'/)
  assert.ok(panel.includes('currentBot.value?.feishu_avatar_url'))
  assert.match(panel, /trustedFeishuAvatarUrl/)
  assert.ok(identity.includes("parsed.protocol === 'https:'"))
  assert.doesNotMatch(identity, /data:image\/png|base64/)
  assert.doesNotMatch(panel, />飞<|>飞\s*<\/span>/)
  assert.match(panel, /class="connected-card__icon"[\s\S]{0,240}data-testid="feishu-binding-identity"/)
  assert.ok(identityMark.includes('failedIdentityKey'))
  assert.match(identityMark, /crossorigin="anonymous"/)
  assert.match(panel, /return `\$\{props\.tabId\}\\n\$\{botId\}\\n\$\{avatarUrl\}`/)
  assert.ok(identityMark.includes('failedIdentityKey.value !== props.identityKey'))
  assert.ok(binding.includes("currentBot.value?.feishu_name||currentBot.value?.name||'Bot'"))
})

test('Feishu message provenance resolves a bound Bot avatar before the outline brand mark', () => {
  assert.ok(userMessage.includes('resolvedFeishuSourceAvatarUrl(turn)'))
  assert.ok(actions.includes('bot.app_id === turn.feishu?.appId'))
  assert.ok(timeline.includes('feishuBotPoolStore.pool?.pool_revision'))
  assert.match(userMessage, /<FeishuIdentityMark[\s\S]{0,160}conversation-message-source__mark/)
  assert.ok(userMessage.includes(':identity-key="feishuSourceIdentityKey(turn)"'))
  assert.doesNotMatch(userMessage, /conversation-message-source__mark[\s\S]{0,120}>\s*飞/)
})

const eventValues = readFileSync(new URL('../src/features/chat/timeline/eventValues.ts', import.meta.url), 'utf8')

test('the unbound panel offers the private pairing flow and a group path side by side', () => {
  for (const marker of [
    'data-testid="feishu-binding-path-private"',
    'data-testid="feishu-binding-path-group"',
    'data-testid="feishu-binding-group-select"',
    'data-testid="feishu-binding-bind-group"',
    'data-testid="feishu-binding-load-groups"',
    'data-testid="feishu-binding-reload-groups"',
    'aria-label="Group"',
    'Anyone in this group can talk to this Chat.',
    'Bind this group',
  ]) {
    assert.ok(panel.includes(marker), marker)
  }
  // Both ways in stay real buttons with a pressed state, so they are keyboard
  // reachable and announce which path is active.
  for (const path of ['private', 'group']) {
    assert.match(panel, new RegExp(
      `<button\\s+type="button"[\\s\\S]{0,200}:aria-pressed="connectMode === '${path}'"` +
      `[\\s\\S]{0,120}@click="connectMode = '${path}'"`,
    ), path)
  }
  assert.ok(panel.includes('Generate pairing code'), 'the existing pairing action remains')
  // The group list is read only when the group path (or a group binding) needs it.
  assert.match(panel, /ensureGroups\(\)/)
  assert.doesNotMatch(panel, /setInterval/)
})

test('a stale group selection cannot survive a Bot switch or a reloaded list', () => {
  // The chosen chat id belongs to one Bot: it must be dropped when the Bot
  // changes or the option disappears, or "Bind this group" submits a chat the
  // user never picked in that Bot's list.
  assert.match(panel, /watch\(selectedBotId, \(\) => \{ selectedChatId\.value = null \}\)/)
  assert.ok(panel.includes('!value.some(chat => chat.chat_id === selectedChatId.value)'))
  assert.ok(panel.includes("selectedChatId.value = null"))
})

test('the topic hint follows the server-resolved form, not just chat_mode', () => {
  assert.ok(panel.includes("chat.is_topic_mode ?? chat.chat_mode === 'topic'"))
  assert.ok(panel.includes("boundGroup.value?.is_topic_mode"))
})

test('the bound panel names a private chat or a group, with the topic hint', () => {
  assert.match(panel, /data-testid="feishu-binding-target"[\s\S]{0,80}\{\{ boundTargetLabel \}\}/)
  assert.match(panel, /binding\.value\?\.chat_type === 'group'/)
  assert.ok(panel.includes('Private chat'))
  assert.ok(panel.includes('Group · '))
  assert.ok(panel.includes('binding.value?.chat_name'))
  assert.match(panel, /v-if="boundIsTopic"[\s\S]{0,180}data-testid="feishu-binding-topic-hint"/)
  assert.ok(panel.includes('Replies stay inside the group topic.'))
  assert.ok(panel.includes("chat.chat_mode === 'topic'"))
  assert.ok(panel.includes('New private messages enter this Chat.'))
  assert.match(binding, /groups,groupsLoaded,groupsLoading,groupsError/)
  assert.ok(binding.includes('listChats'))
  assert.ok(binding.includes('requestGroupBind'))
})

test('a group message badge appends a compact scope suffix and leaves p2p alone', () => {
  assert.match(userMessage, /\{\{ messageSourceLabel\(turn\) \}\}/)
  assert.match(userMessage, /v-if="messageSourceScope\(turn\)"[\s\S]{0,180}data-testid="conversation-message-scope"/)
  assert.ok(userMessage.includes('· {{ messageSourceScope(turn) }}'))
  assert.ok(eventValues.includes("feishu.chatType !== 'group'"))
  assert.ok(eventValues.includes("threadId ? 'group topic' : 'group'"))
})

test('mounted Bot settings dialog disables mode switching with an inline reason while channels exist', async t => {
  const h = harness(t)
  const bound = bot('b', {
    mode: 'single',
    bindings: [activeBinding('tab-active', { bot_id: 'b', app_id: 'app-b' })],
  })
  const initial = pool(1, [bot('a'), bound])
  h.store.applyPool(initial)
  h.setServer(initial)
  const state = await mountSettings(t, h)

  state.select(h.store.botById('a'))
  await settleUi()
  const singleButton = findByTestId(state.$root, 'feishu-bot-mode-single')
  const perChannelButton = findByTestId(state.$root, 'feishu-bot-mode-per-channel')
  assert.ok(singleButton && perChannelButton)
  assert.equal(singleButton.props.disabled, false)
  assert.equal(perChannelButton.props.disabled, false)
  assert.equal(singleButton.props['aria-pressed'], true)
  assert.equal(perChannelButton.props['aria-pressed'], false)
  assert.match(textOf(singleButton), /This Bot serves one Chat/)
  assert.match(textOf(perChannelButton), /Everyone who talks to this Bot gets their own Chat/)
  assert.equal(findByTestId(state.$root, 'feishu-bot-mode-disabled-reason'), null)

  await clickNode(perChannelButton)
  assert.equal(state.editMode, 'per_channel')
  assert.equal(perChannelButton.props['aria-pressed'], true)

  state.select(h.store.botById('b'))
  await settleUi()
  const disabledSingle = findByTestId(state.$root, 'feishu-bot-mode-single')
  const disabledPerChannel = findByTestId(state.$root, 'feishu-bot-mode-per-channel')
  const reasonNode = findByTestId(state.$root, 'feishu-bot-mode-disabled-reason')
  assert.equal(disabledSingle.props.disabled, true)
  assert.equal(disabledPerChannel.props.disabled, true)
  assert.ok(reasonNode, 'disabled reason must be rendered in the dialog')
  assert.match(textOf(reasonNode), /Switching mode requires no active channels.*Disconnect/i)
})

test('mounted binding panel renders per_channel rows from bindings[] without a phantom projection row and releases one row by pairing_id', async t => {
  const dmBinding = activeBinding('tab-dm', {
    pairing_id: 'pair-dm', chat_type: 'p2p', chat_name: 'ou_sender_1',
  })
  const groupBinding = activeBinding('tab-1', {
    pairing_id: 'pair-group', chat_type: 'group', chat_id: 'oc-group-1', chat_name: 'Release room',
  })
  const h = harness(t)
  const snapshot = pool(2, [
    bot('a', {
      revision: 2,
      mode: 'per_channel',
      // Both `binding` (first-element projection) and `bindings` are present;
      // the panel must render two rows, not three.
      binding: dmBinding,
      bindings: [dmBinding, groupBinding],
    }),
  ])
  h.store.applyPool(snapshot)
  h.setServer(snapshot)
  h.setTransport(async (url, init = {}) => {
    if (url.endsWith('/chats')) return response(chatList([groupChat('oc-group-1', { name: 'Release room' })], 2))
    if (init.method === 'DELETE' && url.endsWith('/a/pairing')) {
      const next = pool(3, [
        bot('a', {
          revision: 3,
          mode: 'per_channel',
          binding: groupBinding,
          bindings: [groupBinding],
        }),
      ])
      h.setServer(next)
      return response(next)
    }
    return response(h.server())
  })

  const mounted = await mountBindingPanel(t, h, 'tab-1')
  await clickNode(findByTestId(mounted.root, 'feishu-binding-trigger'))

  assert.equal(findByTestId(mounted.root, 'feishu-binding-target'), null, 'single-target row is replaced in per_channel mode')
  const rows = findAllByTestId(mounted.root, 'feishu-binding-channel-item')
  assert.equal(rows.length, 2, 'renders exact bindings[] items with no duplicate from binding projection')
  assert.match(textOf(rows[0]), /DM · ou_sender_1/)
  assert.match(textOf(rows[0]), /Chat\s*tab-dm/)
  assert.match(textOf(rows[1]), /Group · Release room/)
  assert.match(textOf(rows[1]), /Chat\s*tab-1/)

  const releaseButtons = findAllByTestId(mounted.root, 'feishu-binding-channel-release')
  assert.equal(releaseButtons.length, 2)
  assert.equal(releaseButtons[0].props['aria-label'], 'Release DM · ou_sender_1')
  assert.equal(releaseButtons[1].props['aria-label'], 'Release Group · Release room')
  assert.equal(
    textOf(findByTestId(mounted.root, 'feishu-binding-panel')).includes('Disconnect Feishu'),
    false,
    'bulk Disconnect Feishu is hidden in per_channel mode in favour of per-row release',
  )

  await clickNode(releaseButtons[0])
  const deleteCall = h.calls.find(call => call.init.method === 'DELETE' && call.url.endsWith('/a/pairing'))
  assert.ok(deleteCall, 'clicking per-row Release must send DELETE /pairing')
  assert.deepEqual(JSON.parse(deleteCall.init.body), {
    expected_revision: 2,
    pairing_id: 'pair-dm',
  })
  const remainingRows = findAllByTestId(mounted.root, 'feishu-binding-channel-item')
  assert.equal(remainingRows.length, 1)
  assert.match(textOf(remainingRows[0]), /Group · Release room/)
})

test('mounted binding panel omits tab_id when binding a group in per_channel mode, sends tab_id in single mode, and explains the channel cap', async t => {
  const h = harness(t)
  const dmBinding = activeBinding('tab-dm', {
    pairing_id: 'pair-dm', chat_type: 'p2p', chat_name: 'ou_sender_1',
  })
  const initial = pool(2, [
    bot('a', { revision: 2, mode: 'per_channel', bindings: [dmBinding] }),
    bot('b', { revision: 4, mode: 'single', bindings: [] }),
  ])
  h.store.applyPool(initial)
  h.setServer(initial)

  h.setTransport(async (url, init = {}) => {
    if (url.endsWith('/chats')) {
      return response(chatList([groupChat('oc-group', { name: 'Design group' })], 2))
    }
    if (init.method === 'POST' && url.endsWith('/bindings/group')) {
      return response(h.server())
    }
    return response(h.server())
  })

  const mounted = await mountBindingPanel(t, h, 'tab-1')
  await clickNode(findByTestId(mounted.root, 'feishu-binding-trigger'))

  // per_channel Bot 'a' is selected on unbound tab-1; groups auto-load when opened.
  const groupSelect = findByTestId(mounted.root, 'feishu-binding-group-select')
  assert.ok(groupSelect, 'group picker is rendered directly in per_channel mode')
  await changeSelect(groupSelect, 'oc-group')
  const bindButton = findByTestId(mounted.root, 'feishu-binding-bind-group')
  assert.equal(bindButton.props.disabled, false)
  await clickNode(bindButton)

  const groupCalls = h.calls.filter(call => call.init.method === 'POST' && call.url.endsWith('/bindings/group'))
  assert.equal(groupCalls.length, 1)
  assert.equal(groupCalls[0].url, '/api/feishu/bot/bots/a/bindings/group')
  const perChannelPayload = JSON.parse(groupCalls[0].init.body)
  assert.deepEqual(perChannelPayload, { chat_id: 'oc-group', expected_revision: 2 })
  assert.equal('tab_id' in perChannelPayload, false)

  // At MAX_BOT_CHANNELS (8), Bind this group is disabled and the panel explains why.
  const fullBindings = Array.from({ length: config.MAX_BOT_CHANNELS }, (_, idx) =>
    activeBinding(`tab-ch-${idx + 1}`, { pairing_id: `pair-${idx + 1}`, chat_name: `ou_${idx + 1}` }),
  )
  const capped = pool(3, [
    bot('a', { revision: 3, mode: 'per_channel', bindings: fullBindings }),
    bot('b', { revision: 4, mode: 'single', bindings: [] }),
  ])
  h.store.applyPool(capped)
  h.setServer(capped)
  await settleUi()
  const capReason = findByTestId(mounted.root, 'feishu-binding-channel-cap-reason')
  assert.ok(capReason, 'channel cap reason must be shown when at MAX_BOT_CHANNELS')
  assert.match(textOf(capReason), new RegExp(`${config.MAX_BOT_CHANNELS}-channel limit`))
  await changeSelect(findByTestId(mounted.root, 'feishu-binding-group-select'), 'oc-group')
  assert.equal(findByTestId(mounted.root, 'feishu-binding-bind-group').props.disabled, true)

  // Switch to single-mode Bot 'b', open the Group chat path, and bind the group: tab_id is required.
  const botSelect = findByTestId(mounted.root, 'feishu-binding-panel').children
    .flatMap(node => node.children ?? [])
    .find(node => node.tag === 'select' && node.props['aria-label'] === 'Bot')
  await changeSelect(botSelect, 'b')
  await clickNode(findByTestId(mounted.root, 'feishu-binding-path-group'))
  await changeSelect(findByTestId(mounted.root, 'feishu-binding-group-select'), 'oc-group')
  await clickNode(findByTestId(mounted.root, 'feishu-binding-bind-group'))

  const singleCalls = h.calls.filter(call => call.init.method === 'POST' && call.url.endsWith('/b/bindings/group'))
  assert.equal(singleCalls.length, 1)
  assert.deepEqual(JSON.parse(singleCalls[0].init.body), {
    chat_id: 'oc-group',
    tab_id: 'tab-1',
    expected_revision: 4,
  })
})
