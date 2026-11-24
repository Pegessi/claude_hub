# Topic-group Web reply protocol

Base: d62f018b. Evidence was fetched with GET only from the authenticated
Messenger page, its app-shell manifest, Messenger scripts and the worker chunk
map. No real messages, chat creation, service restarts or pushes were performed.
Private source URLs, exact offsets, complete official request definition and
bundle hashes are retained under `/tmp/feishu-topic-evidence-1124/`.

## Verified protocol

Worker 5492 declares cmd 5 `messages.PutMessageRequest`: fields 1 through 22,
plus 101, 102 and 103. There is no `threadId` field. Missing scalar fields were
added together with the eight referenced context definitions; existing content
and rich-text definitions remain shared. Field 8 is `sendToChat` (default true),
10 `isThreadGroupTopic`, 11 `isAnonymous`, 13 `isEncrypted`, 15 `isShare`,
16 `aiInfoContext`, 17 `syncToChat`, 18 `partialReplyInfo`, 19 `isSmartReply`,
20 `isMentionDoubao`, 21 `larkAgentContext`, 22 `urlPreviewContext`.

Worker 2326 module 1062 constructs the request with rootId, parentId and
isReplyInThread; it does not pass a threadId. Messenger's share-target
constructor uses rootId=parentId=threadId for a topic target and sets
isReplyInThread only when the target is a normal-chat message thread
(`msgThread`), not `threadTopic`. Lazy Messenger chunk 80833 supplies the ordinary topic editor:
`appendThreadMessage` sets rootId=parentId=thread.rootMessageId and
threadId=thread.id. The detail component sets isReplyInThread only for
ReplyThreadDetailPage/ThreadDetailPage/MyAiChatThread namespaces, not topic
namespaces. Topic-group replies therefore use the topic root as both parent
and root; ordinary quoted replies retain their addressed message as parent. Topic chat modes 2 and 3 already
normalize into WebChat.thread; the adapter now uses this to clear wire field 12
while retaining logical topic identity in receipts. Ordinary group/P2P threads
keep field 12 true. Unavailable chat metadata is not silently treated as a
known topic group.

## Safe Bot fallback

`web_bot_chat_ids` in the existing AI-command config maps numeric Web chat IDs
to operator-verified OpenAPI `oc_` IDs, for example
`{"20000":"oc_verified"}`. It is accepted by the existing config API and passed
through the normal client factory. Following a definite Web rejection in auto
mode, mappings are usable only when a configured or enabled pool-bound Bot lists the target group as
visible. Name equality alone never proves identity. The confirming Bot performs the send. Missing/unseen mappings
fail closed. Numeric owners cannot be used as Bot OpenIDs, including the older
not-in-chat fallback paths. Explicit Web mode and uncertain sends retain their
existing boundaries.

No live mapping was invented or installed: an operator must supply verified
identity pairs if the Web chat does not expose openChatId. No live send was
used to claim runtime delivery success.

## Validation

Focused wire/adapter/fallback/config tests, backend-format, backend-types,
check-staged, full-history private-pattern DLP and native independent Codex
review are recorded in the Task report and external delivery report. Full
`verify.sh all` is intentionally excluded by the task contract. The official
request's Content subtree was retained as evidence rather than importing
unrelated attachment/calendar schemas.
