# Nivelle Archive memory retrieval

Phase 2.1 uses a deterministic `sqlite_hybrid` retriever. It does not use an embedding
model, `sqlite-vec`, or semantic vector search.

## Eligibility and states

- Active, explicitly saved, non-superseded memories are eligible for automatic recall.
- Inactive memories stay visible in the library but are excluded from automatic recall.
  An explicitly attached inactive memory may be included. Library search includes them
  only when the user explicitly selects `비활성 포함` (`include_inactive=true`).
- A superseded record stays in SQLite with its original ID and content for auditability,
  but is inactive and never automatically injected.
- Delete remains a physical delete in this schema. Deleted rows are absent from lists,
  FTS, substring search, and prompts. If a request explicitly refers to a missing ID,
  debug context reports that ID as `deleted`; there is no `include_deleted` library mode.

## Normalization and duplicate protection

`normalized_content` is produced by Unicode NFKC normalization, case folding, and
collapsing punctuation and whitespace. An active exact canonical duplicate is rejected
with HTTP 409 and `existing_memory_id`. Updating the same logical record is allowed and
preserves its ID. Content changes append a row to `memory_revisions` and the FTS trigger
removes the previous text before indexing the new text.

Migration v4 repairs pre-existing active duplicates without deleting them. The
highest-priority, newest record is the deterministic canonical record. Other exact
duplicates become inactive and receive `superseded_by=<canonical ID>`.

For non-identical legacy conflicts, the retriever uses a deliberately conservative
logical key only for short single-fact assignments such as `강조색은 회색이다`,
`setting is value`, or `setting: value`. It does not guess that long, multi-clause
hardware notes conflict. An explicitly attached fact wins first; otherwise the newer
`updated_at` wins, followed by relevance and priority. Losing candidates are reported
as `conflict_lost`. Normal edits should still update the existing memory ID instead of
creating a second contradictory row.

## Candidate generation

Candidate generation is bounded by `candidate_limit` and merges these stages:

1. Exact canonical phrase lookup.
2. A feature-detected SQLite FTS5 `trigram` substring index, when the deployed
   SQLite build provides that optional tokenizer.
3. SQLite FTS5 `unicode61` prefix lookup, when FTS5 is available.
4. A controlled `normalized_content LIKE` substring fallback for at most 20 query terms.
5. A bounded high-priority backfill used only to explain rejected decisions.

Active candidates always receive the full `candidate_limit`. Inactive/superseded debug
examples use a separate bounded quota, so a large archive cannot displace a relevant
active fact merely because debug metadata is enabled.

The fallback supports Korean partial forms such as `히냥이` matching `히냥이이다`.
Small synonym groups cover project vocabulary such as 호칭/이름, 사양/구성/스펙,
RAM/램/메모리, and model/LLM/Qwen. This is not presented as morphological analysis.
The generic project name `Nivelle/니벨` is a query stopword, so it cannot make every
project note relevant to a specific question. When a query names only the server or
only the client, a hardware note naming only the opposite PC receives an entity-scope
penalty; architecture notes that explicitly discuss both PCs remain eligible.

## Ranking

Each candidate receives values between zero and one:

```text
final_score = relevance_score * 0.70
            + priority_score  * 0.20
            + recency_score   * 0.10
```

`priority_score` is `priority / 100`. `recency_score` uses exponential decay with a
365-day time constant. Relevance uses token/group coverage plus configurable exact,
prefix, and substring boosts. A candidate below `minimum_relevance` is excluded even
when its priority is high. Explicit attachments rank first; the total selection still
respects `top_k`.

Defaults are stored under `memory_retrieval` in `config/examples/memory.yaml`. Pydantic
requires the three weights to total 1.0 and `candidate_limit >= top_k`.

## Observability contract

`MemoryRetriever.retrieve()` returns `selected` and `rejected` decisions. Every context
item contains `memory_id`, safe `summary`, category, priority, relevance/priority/
recency/final scores, `included`, and `reason`. Supported reasons include `selected`,
`explicitly_attached`, `inactive`, `deleted`, `superseded`, `duplicate`,
`low_relevance`, and `top_k_limit` (plus reserved privacy/conflict reasons).

Only `selected` content may be composed into the model prompt. `assistant.context`
may expose both lists so the client can prove what was and was not used. Obvious token,
password, email, and phone patterns are redacted from summaries.
