# Nivelle Core v2

## 목적

Nivelle의 정체성, Persona, Memory, Conversation, 사용자 맥락 및 Tool Policy를 특정 모델이나 추론 엔진에서 분리한다. 모델, 음성, 비전, 데이터베이스 및 UI는 교체 가능한 구현 세부사항이다.

## 현재 요청 흐름

```text
Nivelle Link
  -> Core WebSocket / request identity
  -> Conversation history + Memory retrieval + Persona context
  -> optional tool planning and policy-controlled execution
  -> ModelRequest
  -> ModelRouter
       -> primary ModelProvider
       -> fallback ModelProvider (안전한 경우에만)
  -> durable assistant message state
  -> one assistant.completed event
```

Core가 소유하는 영역:

- request/message/conversation identity와 중복 방지
- Conversation 및 Memory 저장
- Persona prompt 조립
- Tool policy, 승인, 실행, 감사
- assistant message의 authoritative 완료/중단 상태

Model Runtime이 소유하는 영역:

- provider/model 선택과 상태 확인
- provider 오류 분류
- 부분 출력이 시작되기 전의 제한된 fallback
- provider stream의 중복 final 억제
- 모델 요청 수명주기와 안전한 메타데이터 로그
- 활성 provider 취소 전달

Provider가 소유하지 않는 영역:

- Persona, Memory 검색, Emotion 분석
- Conversation history 저장
- Tool 허용 여부와 UI 이벤트
- assistant message의 영구 완료 상태

## 확장 경계

`ModelProvider`는 텍스트 중심의 최소 계약이며 `ModelCapabilities`로 streaming, tool calling, vision, structured output 가능 여부를 표현한다. 미래의 Voice, Vision, Emotion Context Engine, Acoustic Lab, DryLab 및 전문 subsystem은 Conversation/Context 계층에서 입력을 구성하되 provider 구현에 Nivelle의 정책을 넣지 않는다.

## 현재 제한

- managed launcher는 한 개의 로컬 llama-server process만 시작한다.
- fallback은 모델별 `endpoint`가 있거나 여러 model ID를 처리하는 OpenAI-compatible endpoint일 때 실제로 분리된 모델에 도달한다.
- tool planning은 현재 primary provider에서만 수행한다. 생성 fallback과 tool execution policy는 분리되어 있다.
- Emotion, Voice, Vision, IoT, unrestricted shell 및 자율 연구 agent는 v2.0 범위가 아니다.
