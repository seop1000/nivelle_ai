# Nivelle chat protocol events

Nivelle Phase 2.1 uses protocol `1.x`. A Link and Core are wire-compatible when
their protocol major versions match. Patch or minor differences may produce a
compatibility warning, but do not by themselves reject a connection.

## `chat.request`

Required fields are `type`, `protocol_version`, `request_id`, and `content`.
Phase 2.1 clients also send a stable `client_message_id`. An older client that omits
it remains compatible: the server uses `request_id` as that message's stable ID.

```json
{
  "type": "chat.request",
  "protocol_version": "1.0",
  "request_id": "uuid",
  "client_message_id": "uuid",
  "retry_of_client_message_id": null,
  "conversation_id": "uuid-or-null",
  "content": "현재 어떤 서버에 연결되어 있어?",
  "runtime_context": {
    "profile_id": "primary",
    "connection_type": "local",
    "host": "192.0.2.10",
    "port": 8765,
    "tls": false,
    "client_version": "0.3.1",
    "latency_ms": 62.49
  }
}
```

`runtime_context` is optional, strictly validated, and used only to answer the
conversation. It never authorizes a client or changes server security decisions.
Credentials and authorization headers are not valid runtime-context fields.

`client_message_id` is persisted on the user message under a unique database
index. Repeating it does not save another message or begin another generation; the
server returns `DUPLICATE_MESSAGE`. The client must not automatically resend an
uncertain in-flight message after reconnecting. A deliberate retry uses a new
`request_id` and `client_message_id`, and may reference the old ID with
`retry_of_client_message_id`. Only an `interrupted` request can be retried, only in its
original conversation, and each original request may have one retry child. The relation
is persisted under a partial unique index, not inferred only from in-memory state.

## Successful event order

For a normal request, events have this order:

1. `chat.accepted`
2. `assistant.context`
3. zero or more `assistant.delta`
4. `assistant.completed`

`assistant.context` is always sent before the first delta. Its correlation fields
include the request ID in the envelope and conversation, user-message,
assistant-message, and client-message IDs in the payload.

```json
{
  "type": "assistant.context",
  "protocol_version": "1.0",
  "request_id": "uuid",
  "payload": {
    "conversation_id": "uuid",
    "user_message_id": "uuid",
    "assistant_message_id": "uuid",
    "client_message_id": "uuid",
    "query": "서버 PC 메모리 배분은?",
    "retrieval": {
      "backend": "sqlite_hybrid",
      "top_k": 5,
      "candidate_count": 8
    },
    "memories": [
      {
        "memory_id": "uuid",
        "summary": "서버 PC의 시스템 RAM은 16GB이고 GPU 예약 메모리는 8GB이다.",
        "category": "project",
        "priority": 90,
        "relevance_score": 0.94,
        "priority_score": 0.90,
        "recency_score": 0.72,
        "final_score": 0.90,
        "included": true,
        "reason": "selected"
      }
    ]
  }
}
```

The `memories` array contains selected and rejected candidates. Only entries with
`included: true` are placed in the model prompt. Allowed exclusion reasons are
`inactive`, `deleted`, `superseded`, `duplicate`, `low_relevance`, `top_k_limit`,
`sensitive`, `conflict_lost`, and `explicitly_excluded`. Safe summaries are sent
instead of known credential-like content.

Clients should accept the earlier `chat.context` event name and its reduced
`conversation_id`/`memories` payload when connecting to an older server. Phase 2.1
servers emit only the canonical `assistant.context` event, avoiding duplicate UI
updates.

## Completion and interruption

`assistant.completed.payload.metrics` contains typed generation data:

- `prompt_tokens`, `completion_tokens`, and `total_tokens`
- `tokens_per_second`
- `first_token_latency_ms` and `total_latency_ms`
- `finish_reason`, `interrupted`, `model`, and `request_id`

Unavailable backend values remain JSON `null`; they are never guessed. The server
allocates the assistant message before streaming. A cancellation, provider error,
or disconnected WebSocket changes a still-`generating` message to `interrupted` and
does not mark it completed. If completion was already committed, database truth wins
and a late disconnect cannot regress it. On an unclean server restart, remaining
`generating` rows are recovered as `interrupted`. `chat.cancelled` and
`LLM_STREAM_INTERRUPTED` include the stable
assistant message ID and interruption metrics when available.

## Relevant errors

- `INVALID_REQUEST`: schema validation failed.
- `PROTOCOL_VERSION_MISMATCH`: protocol major versions differ or are invalid.
- `DUPLICATE_REQUEST`: the same request ID is already running on this socket.
- `DUPLICATE_MESSAGE`: the stable client-message ID was already received.
- `RETRY_TARGET_NOT_FOUND`: the referenced original request is unknown.
- `RETRY_TARGET_NOT_INTERRUPTED`: the referenced request is completed or still active.
- `RETRY_CONVERSATION_MISMATCH`: the retry names a different conversation.
- `RETRY_ALREADY_CREATED`: the original request already has or is allocating a retry.
- `CONVERSATION_BUSY`: another generation owns the conversation.
- `CONVERSATION_NOT_FOUND`: the conversation is unknown or archived.
- `PROMPT_TOO_LARGE`: fixed prompt content exceeds the configured context budget.
- `LLM_STREAM_INTERRUPTED`: generation stopped before completion.

No event or log may include authentication tokens, Authorization headers, pairing
codes, passwords, or private keys.
