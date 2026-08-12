# Nivelle Protocol v1

REST lives below `/api/v1`; chat uses `/ws/v1/chat`. JSON envelopes carry
`protocol_version: "1.0"` and `request_id`. Authenticated calls use
`Authorization: Bearer <token>`, including the WebSocket handshake. Tokens are
never accepted in query strings.

Client events: `chat.request`, `chat.cancel`, and `ping`. Server events:
`chat.accepted`, `assistant.delta`, `assistant.completed`, `chat.cancelled`,
`server.status`, `error`, and `pong`. Unknown versions or event types produce a
structured error rather than closing without explanation.

Errors have the shape:

```json
{"error":{"code":"MODEL_UNAVAILABLE","message":"모델을 사용할 수 없습니다.","details":{},"request_id":"...","retryable":true}}
```

An assistant completion includes conversation/message IDs, final content, token
counts when supplied by the provider, and finish reason. Deltas are ordered by a
monotonic `sequence`. The server commits only the completed assistant message.
