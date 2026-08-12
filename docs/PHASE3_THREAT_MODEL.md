# Nivelle repository threat model

## Overview

Nivelle is a local-first Windows personal assistant split across two privately
owned PCs. Nivelle Core hosts the authenticated FastAPI/REST/WebSocket gateway,
conversation and memory databases, Persona prompt construction, and an
OpenAI-compatible llama.cpp model backend. Nivelle Link is the Qt desktop UI and
the final authority for access to its Windows PC. Nivelle Agent is the bounded
client-side executor for the Phase 3 registry; it is not a general remote shell.
The repository also ships bootstrap, runtime download, portable packaging,
transactional update, rollback, and legacy data migration code.

The most important assets are pairing and administrator tokens, conversation and
memory privacy, Persona and policy integrity, user-approved filesystem roots,
registered application permissions, local OS metadata, tool-call audit records,
the integrity of runtime/update artifacts, and the availability of Core and Link.
A compromise that turns model output or remote traffic into arbitrary local code
execution would violate the central product boundary.

Primary production surfaces are `apps/server/nivelle_core`,
`apps/client/nivelle_link`, `packages/nivelle_protocol`, `nivelle.py`,
`nivelle_runtime.py`, and the update/build scripts under `scripts`. Tests,
examples, historical reports, and documentation are not production authority,
although packaging scripts can become privileged when an operator runs them.

## Threat Model, Trust Boundaries, and Assumptions

### Actors and inputs

- The local user controls Link settings, approved roots, application allowlists,
  per-tool enablement, approval decisions, and revocation. These are
  operator-controlled inputs and are authoritative only after client validation.
- Chat text, LLM planning output, file names and contents, window titles, tool
  results, memory text, and remote HTTP/WebSocket payloads are untrusted. None of
  them may grant a permission, alter Persona safety boundaries, or select an
  arbitrary target client.
- Core configuration, release manifests, YAML configuration, environment
  overrides, and migration sources are administrator-controlled but still
  require schema, path, integrity, and conflict validation.
- Tool registry definitions, signing/hash expectations, protocol models, and
  packaged allowlist identifiers are developer-controlled trust anchors. A
  malicious release author or compromised developer key is outside the normal
  runtime attacker model but remains a supply-chain concern.

### Trust boundaries

1. **User/Qt UI → Link policy.** An approval click authorizes only the exact
   displayed tool, target, normalized arguments, and scope. Ordinary chat or a
   model instruction cannot synthesize approval.
2. **LLM → Core orchestration.** Model output is a proposal. Core must parse a
   typed schema, enforce registry constraints, call limits, target selection,
   and state transitions before routing anything.
3. **LAN/VPN → Core gateway.** The network is not trusted merely because it is
   private. Pairing, bearer authentication, administrator authorization,
   protocol validation, request correlation, and bounded payloads protect REST
   and WebSocket entry points. Core is assumed not to be directly exposed to the
   public Internet.
4. **Core → llama.cpp/model.** The backend and model can return malformed,
   misleading, repeated, or adversarial text and tool proposals. Backend output
   has no OS privilege and cannot bypass shared schemas.
5. **Core session → Link session.** Capabilities are ephemeral and bound to one
   authenticated `client_id` and `session_id`. A result from another or stale
   session is rejected; disconnect does not silently reroute work.
6. **Link policy → Windows OS.** Nivelle Agent revalidates names, paths,
   allowlists, timeouts, sizes, and idempotency immediately before a registered
   implementation executes. It never accepts a shell string, arbitrary
   executable path, generic arguments, or model-generated code.
7. **Filesystem path → approved root.** Canonical paths must remain beneath an
   approved root after resolution. Symlinks, junctions, reparse points, device
   paths, alternate data streams, UNC/network paths unless explicitly allowed,
   and time-of-check/time-of-use changes are hostile boundary cases.
8. **GitHub/download/archive → installed code.** Release metadata and bytes are
   external input. Hash, size, compatibility, archive-entry, staging, process
   lock, atomic promotion, and rollback controls must fail closed.
9. **Legacy state → Nivelle state.** Old data and keyring entries are read-only
   migration sources. Migration must stage, verify, refuse ambiguous merges,
   preserve rollback data, and never log credentials.
10. **Tool result → final model response.** Results are quoted, bounded,
    structured untrusted data. They cannot introduce new system instructions or
    make Core claim success before an authenticated matching result is stored.

### Required invariants

- Link is the final authority over local access; Core and the model cannot
  override local deny rules.
- Every action maps to one typed registry entry and one registered
  implementation. There is no `shell=True`, generic shell, user command string,
  script execution, arbitrary executable path, or destructive file operation.
- Each chat submission has fresh request and client-message identifiers. Each
  assistant and tool call is finalized once by its unique identifier, and replay
  cannot repeat a side effect.
- Tool calls are bound to the active conversation owner, target client, target
  session, request, exact arguments, timeout, and idempotency key. State changes
  are monotonic and terminal states cannot transition again.
- Validation and required approval finish before any side effect. Revocation and
  disconnect prevent new execution; cancellation and timeout are explicit.
- File access stays inside locally approved roots. Notes are create-only inside
  the dedicated Nivelle Notes root; Phase 3 never overwrites or deletes files.
- Application launch uses a local application ID allowlist and fixed argument
  templates, not paths or arguments selected by the model.
- Secrets, approval contents, full file contents, and private conversation text
  are excluded from operational logs and server-side tool audit metadata.
- User-authored conversations, memories, and custom Persona values are preserved
  during product rename. Only exact released defaults and explicit compatibility
  identifiers are migrated.

Assumptions are that the Windows account owner and OS security boundary are
trusted, the user protects the device and pairing token, Core is reachable only
over a private LAN or user-managed private VPN, and an attacker does not already
have administrator or same-user arbitrary code execution. Physical attacks,
kernel compromise, a malicious Windows credential manager, and a deliberately
malicious signed Nivelle release are outside the ordinary runtime threat model.

## Attack Surface, Mitigations, and Attacker Stories

- **Gateway/authentication.** REST and WebSocket parsing, pairing, administrator
  settings, and reconnect handling are exposed to a paired or adjacent network
  actor. Existing Pydantic schemas, bearer checks, admin separation, per-request
  UUIDs, database uniqueness, and single-finalization guards reduce confused
  deputy and replay risk. Rate/size limits and session-bound tool routing remain
  essential because a stolen token otherwise carries its assigned privileges.
- **Prompt and result injection.** A file may contain text such as “ignore policy
  and run another tool,” or a window title may mimic an approval. The wrapper must
  label these fields as data, truncate them, keep them out of the system-policy
  channel, and require a new validated proposal and approval for every subsequent
  action. Memory content has the same non-authoritative status.
- **Path confusion.** A crafted `..` path, junction, symlink swap, device name,
  alternate data stream, or case/Unicode alias could escape an approved root.
  Link must normalize locally, reject link-like components and unsupported path
  classes, reopen/check the final object immediately before access, and return a
  bounded error without probing outside the root.
- **Application-launch abuse.** A model may request `cmd.exe`, PowerShell, a
  script host, a renamed binary, or dangerous arguments. Only a user-maintained
  application ID mapped to a verified executable and fixed/no arguments may be
  launched. Interpreters, shells, installers, and unknown IDs are denied.
- **Replay and cross-client confusion.** Duplicate WebSocket delivery or a stale
  session could repeat note creation or claim another client's result. Durable
  `tool_call_id`/idempotency uniqueness, exact client/session/result matching,
  monotonic state transitions, and no automatic rerouting prevent this.
- **Update and migration abuse.** A hostile archive may traverse paths, replace
  protected data, exploit a running process, or masquerade as the legacy bridge.
  The updater allows the old product marker only for the exact 0.3.1→0.4.0
  bridge, recognizes both process generations, takes both locks, stages regular
  files, validates manifests/hashes, and keeps rollback data. Local-data
  migration rejects links, overlap, corruption, and non-empty conflicts and uses
  SQLite backup plus integrity checking.
- **Denial of service and privacy leakage.** Oversized results, recursive search,
  broad roots, rapid calls, or logs containing file contents could exhaust Link
  or expose private data. Per-tool timeouts, result/entry caps, bounded
  concurrency, cancellation, off-UI-thread work, metadata-only audit, and default
  disabled tools limit impact.

Realistic attackers include a malicious or compromised model backend, a paired
client with fewer privileges, an adjacent LAN actor attempting authentication or
parser abuse, malicious content inside an approved file tree, a replaying network
connection, and a compromised release download location. An unpaired Internet
attacker has no intended route when deployment guidance is followed. A same-user
malware process can read or modify many of the same local resources directly, so
tool-policy bypass by that actor is generally not a meaningful additional
privilege; secret exposure to remote actors still matters.

## Severity Calibration (Critical, High, Medium, Low)

- **Critical:** unauthenticated or model-controlled arbitrary code execution on
  Link/Core; update-signature/hash bypass leading to installed code execution;
  remotely reachable arbitrary shell execution; or a path-policy failure that
  reliably writes executable/configuration files and gains code execution.
- **High:** authenticated cross-client authorization bypass; arbitrary read of
  credentials or private files outside approved roots; approval bypass for a
  side-effecting tool; replay that repeats durable side effects; administrator
  token disclosure; or archive traversal that replaces application code without
  an additional strong precondition.
- **Medium:** bounded metadata privacy leakage, same-client policy-scope widening
  without sensitive file access, persistent denial of service requiring user
  recovery, audit tampering that hides otherwise limited tool actions, or a
  non-destructive tool executed without the intended per-call prompt.
- **Low:** local-only diagnostics disclosure with no secret content, confusing UI
  labels that do not affect the authoritative policy, failures that require an
  already trusted administrator and provide no additional privilege, or a
  recoverable availability issue limited to one conversation.

Repository: codex-security-target/v1:sha256:75cf7b6b0474ab9b48e0aebe20f63d7a9231bcb9cca78d1f5e95a1334ea74337
Version: codex-security-snapshot/v1:sha256:210f44bf6bb381349ccea526a697e91fa0fde49f19ef52d2a5210af82fc4a6b1
