"""Deterministic SQLite-backed long-term memory retrieval.

The retriever deliberately does not claim semantic/vector search.  It merges
FTS5, prefix and bounded substring candidates supplied by ``MemoryRepository``
and applies an explainable score in Python.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nivelle_protocol.memory import (
    MemoryContextItem,
    MemoryDecisionReason,
    MemoryRecord,
    MemoryRetrievalSettings,
)

if TYPE_CHECKING:
    from .memory_repository import MemoryRepository

_TOKEN_PATTERN = re.compile(r"[0-9a-z가-힣]+", re.IGNORECASE)
_KOREAN_PARTICLES = tuple(
    sorted(
        {
            "으로부터는",
            "에게서는",
            "한테서는",
            "으로부터",
            "에서부터",
            "이라고",
            "이라는",
            "에게서",
            "한테서",
            "께서는",
            "에서는",
            "으로는",
            "부터는",
            "에게는",
            "한테는",
            "처럼",
            "보다",
            "하고",
            "이라",
            "이며",
            "에는",
            "에서",
            "으로",
            "부터",
            "에게",
            "한테",
            "께서",
            "라고",
            "라는",
            "이랑",
            "랑",
            "의",
            "은",
            "는",
            "이",
            "가",
            "을",
            "를",
            "에",
            "와",
            "과",
            "도",
            "만",
        },
        key=len,
        reverse=True,
    )
)
_QUERY_STOPWORDS = {
    "각각",
    "그게",
    "그리고",
    "기억",
    "기억해",
    "나의",
    "내",
    "대해",
    "대해서",
    "되어",
    "된",
    "뭐",
    "뭐야",
    "무엇",
    "사용",
    "설명",
    "알려",
    "알려줘",
    "어떻게",
    "얼마",
    "얼마야",
    "있는",
    "있어",
    "있지",
    "저장",
    "현재",
    "해줘",
    "nivelle",
    "니벨",
    "what",
    "which",
    "please",
    "tell",
    "about",
    "current",
}
_ALIASES: tuple[frozenset[str], ...] = (
    frozenset({"호칭", "닉네임", "이름", "부르", "부를", "불러", "불리"}),
    frozenset({"사양", "구성", "스펙", "spec", "specification"}),
    frozenset({"메모리", "램", "ram"}),
    frozenset({"gpu", "vram", "그래픽", "예약"}),
    frozenset({"클라이언트", "데스크톱", "client"}),
    frozenset({"모델", "llm", "qwen", "큐웬"}),
    frozenset({"2pc", "투피씨", "두대", "2대"}),
    frozenset({"검색", "fts", "fts5", "sqlite", "데이터베이스", "db"}),
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|password|passwd|private[_ -]?key)"
    r"\s*[:=]\s*\S+"
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_EMAIL = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Z]{2,}(?![\w.+-])")
_PHONE = re.compile(r"(?<!\d)(?:\+?82[- .]?)?0(?:1[016789]|2|[3-6][1-5])[- .]?\d{3,4}[- .]?\d{4}(?!\d)")
_KOREAN_ASSIGNMENT = re.compile(
    r"^(?P<subject>.{2,100}?)(?:은|는|이|가)\s*(?P<value>.+?)(?:이다|입니다|이에요|예요)$"
)
_ENGLISH_ASSIGNMENT = re.compile(
    r"^(?P<subject>.{2,100}?)\s+(?:is|are)\s+(?P<value>.+)$",
    re.IGNORECASE,
)
_MULTI_FACT_MARKERS = ("이고", "이며", "그리고", "하지만", " 또는 ", " and ", " but ")
_FACT_PROPERTY_MARKERS = (
    "강조색",
    "색상",
    "color",
    "theme",
    "호칭",
    "닉네임",
    "이름",
    "name",
    "ram",
    "vram",
    "gpu",
    "메모리",
    "memory",
    "모델",
    "model",
    "주소",
    "address",
    "포트",
    "port",
    "버전",
    "version",
    "운영체제",
    "언어",
    "language",
    "경로",
    "path",
)
_ENTITY_TERMS: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
    (frozenset({"서버", "server"}), frozenset({"클라이언트", "client"})),
    (frozenset({"클라이언트", "client"}), frozenset({"서버", "server"})),
)


def normalize_memory_content(value: str) -> str:
    """Return the canonical form used for duplicate checks and LIKE fallback.

    NFKC and case folding happen first.  Unicode punctuation and every kind of
    whitespace are collapsed to a single ASCII space.  Symbols are preserved,
    so facts such as ``C++`` do not become indistinguishable from ``C``.
    """

    normalized = unicodedata.normalize("NFKC", value).casefold()
    characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if character.isspace() or category.startswith(("P", "Z")):
            characters.append(" ")
        elif not category.startswith("C"):
            characters.append(character)
    return " ".join("".join(characters).split())


def _strip_korean_particle(token: str) -> str:
    for particle in _KOREAN_PARTICLES:
        if token.endswith(particle) and len(token) - len(particle) >= 2:
            return token[: -len(particle)]
    return token


def query_term_groups(query: str) -> list[tuple[str, ...]]:
    """Tokenize a query without pretending to be a morphology analyzer."""

    groups: list[tuple[str, ...]] = []
    seen_groups: set[tuple[str, ...]] = set()
    for raw_token in _TOKEN_PATTERN.findall(normalize_memory_content(query)):
        term = _strip_korean_particle(raw_token)
        terms = [term]
        # A tiny deterministic compound fallback covers forms such as
        # "시스템램" without claiming morphological analysis.
        for suffix in ("메모리", "사양", "램"):
            if term.endswith(suffix) and len(term) - len(suffix) >= 2:
                terms = [term[: -len(suffix)], suffix]
                break
        for candidate in terms:
            if len(candidate) < 2 or candidate in _QUERY_STOPWORDS:
                continue
            alias = next((values for values in _ALIASES if candidate in values), None)
            group = tuple(sorted(alias)) if alias is not None else (candidate,)
            if group in seen_groups:
                continue
            seen_groups.add(group)
            groups.append(group)
            if len(groups) >= 12:
                return groups
    return groups


def flatten_query_terms(query: str) -> list[str]:
    return list(dict.fromkeys(term for group in query_term_groups(query) for term in group))


@dataclass(frozen=True)
class TextMatch:
    relevance: float
    exact_phrase: bool
    substring: bool
    prefix: bool


def score_text_match(query: str, content: str, settings: MemoryRetrievalSettings) -> TextMatch:
    groups = query_term_groups(query)
    if not groups:
        return TextMatch(0.0, False, False, False)
    normalized_query = normalize_memory_content(query)
    normalized_content = normalize_memory_content(content)
    content_tokens = set(_TOKEN_PATTERN.findall(normalized_content))
    stripped_tokens = {_strip_korean_particle(token) for token in content_tokens}
    candidate_tokens = content_tokens | stripped_tokens

    prefix_seen = False
    substring_seen = False
    group_scores: list[float] = []
    for variants in groups:
        best = 0.0
        for term in variants:
            if term in candidate_tokens:
                best = max(best, 1.0)
            elif any(token.startswith(term) for token in candidate_tokens):
                best = max(best, 0.85)
                prefix_seen = True
            elif any(term.startswith(token) for token in candidate_tokens if len(token) >= 2):
                best = max(best, 0.72)
                prefix_seen = True
            elif len(term) >= 2 and term in normalized_content:
                best = max(best, 0.65)
                substring_seen = True
        group_scores.append(best)

    exact_phrase = bool(normalized_query and normalized_query == normalized_content)
    phrase_inside = bool(
        normalized_query
        and len(normalized_query) >= 4
        and normalized_query in normalized_content
    )
    relevance = sum(group_scores) / len(group_scores)
    if exact_phrase:
        relevance += settings.exact_phrase_boost
    elif phrase_inside:
        relevance += settings.substring_boost
        substring_seen = True
    if prefix_seen:
        relevance += settings.prefix_boost
    if substring_seen:
        relevance += settings.substring_boost
    # Hardware and connection facts often share terms such as RAM, GPU and PC.
    # When the question names exactly one side of the 2PC topology, a note that
    # names only the opposite side must not be selected just for those shared
    # terms. Notes describing both sides (for example the architecture fact)
    # remain eligible.
    query_terms = set(_TOKEN_PATTERN.findall(normalized_query))
    content_terms = set(_TOKEN_PATTERN.findall(normalized_content))
    for requested, opposite in _ENTITY_TERMS:
        if query_terms & requested and not query_terms & opposite:
            if content_terms & opposite and not content_terms & requested:
                relevance *= 0.1
            break
    return TextMatch(min(round(relevance, 6), 1.0), exact_phrase, substring_seen, prefix_seen)


def safe_memory_summary(content: str, *, limit: int = 240) -> str:
    """Create a UI-safe summary without logging or exposing obvious credentials."""

    summary = " ".join(content.split())
    summary = _SENSITIVE_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[redacted]", summary)
    summary = _BEARER_TOKEN.sub("Bearer [redacted]", summary)
    summary = _EMAIL.sub("[redacted-email]", summary)
    summary = _PHONE.sub("[redacted-phone]", summary)
    return summary if len(summary) <= limit else f"{summary[: limit - 1].rstrip()}…"


def memory_conflict_key(content: str) -> str | None:
    """Return a conservative key for simple single-fact assignments.

    Phase 2.1 must not guess that two merely related notes conflict.  We only
    group short, explicit ``subject = value``-style facts.  Multi-clause notes
    (for example a full hardware specification) intentionally remain separate.
    """

    normalized = normalize_memory_content(content)
    if not normalized or len(normalized) > 180:
        return None
    if any(marker in normalized for marker in _MULTI_FACT_MARKERS):
        return None
    match = _KOREAN_ASSIGNMENT.fullmatch(normalized) or _ENGLISH_ASSIGNMENT.fullmatch(
        normalized
    )
    if match is None:
        if ":" not in content:
            return None
        subject, _separator, value = content.partition(":")
        if not subject.strip() or not value.strip():
            return None
        return normalize_memory_content(subject)
    subject = normalize_memory_content(match.group("subject"))
    value = normalize_memory_content(match.group("value"))
    if not value or not any(marker in subject for marker in _FACT_PROPERTY_MARKERS):
        # A bare entity can have many compatible attributes: "사용자는
        # 개발자이다" and "사용자는 남성이다" must not be treated as a
        # contradiction merely because their grammatical subject is equal.
        return None
    return subject if len(subject) >= 2 else None


def _recency_score(updated_at: datetime, reference: datetime) -> float:
    timestamp = updated_at if updated_at.tzinfo is not None else updated_at.replace(tzinfo=UTC)
    age_days = max((reference - timestamp.astimezone(UTC)).total_seconds() / 86400, 0.0)
    return round(math.exp(-age_days / 365.0), 6)


@dataclass(frozen=True)
class MemoryRetrievalResult:
    backend: str
    top_k: int
    candidate_count: int
    selected: tuple[MemoryContextItem, ...]
    rejected: tuple[MemoryContextItem, ...]

    @property
    def memories(self) -> tuple[MemoryContextItem, ...]:
        return self.selected + self.rejected


@dataclass(frozen=True)
class _RankedCandidate:
    record: MemoryRecord
    item: MemoryContextItem
    explicit: bool


class MemoryRetriever:
    """Retrieve prompt memories and return an auditable inclusion decision."""

    backend = "sqlite_hybrid"

    def __init__(
        self, repository: MemoryRepository, settings: MemoryRetrievalSettings
    ) -> None:
        self.repository = repository
        self.settings = settings

    async def retrieve(
        self,
        query: str,
        *,
        recent_messages: Sequence[str] = (),
        conversation_id: str | None = None,
        explicitly_attached_memory_ids: Sequence[str] = (),
        limit: int | None = None,
        include_debug_metadata: bool = True,
    ) -> MemoryRetrievalResult:
        # conversation_id is intentionally accepted for correlation and future
        # policy hooks. It never changes deterministic ranking in Phase 2.1.
        del conversation_id
        requested_limit = self.settings.top_k if limit is None else limit
        top_k = min(max(requested_limit, 0), 10)
        if not self.settings.enabled or top_k == 0:
            return MemoryRetrievalResult(self.backend, top_k, 0, (), ())

        recent = [
            " ".join(message.split())
            for message in recent_messages[-self.settings.include_recent_user_messages :]
            if message.strip()
        ]
        # The current request is authoritative.  Put it first so the bounded
        # tokenizer cannot consume all term slots with older conversation text.
        # Recent messages broaden candidate recall, but only receive a small
        # secondary relevance contribution below.
        recent_query = " ".join(recent).strip()
        retrieval_query = " ".join([query, recent_query]).strip()
        pool = await self.repository.retrieval_candidates(
            retrieval_query,
            limit=self.settings.candidate_limit,
            include_inactive_debug=include_debug_metadata,
        )
        explicit_ids = list(dict.fromkeys(explicitly_attached_memory_ids))
        explicit_records = await self.repository.get_many(explicit_ids)
        records_by_id = {record.id: record for record in pool}
        records_by_id.update({record.id: record for record in explicit_records})
        explicit_set = {record.id for record in explicit_records}
        reference = datetime.now(UTC)

        ranked: list[_RankedCandidate] = []
        rejected: list[MemoryContextItem] = []
        for record in records_by_id.values():
            current_match = score_text_match(query, record.content, self.settings)
            recent_match = (
                score_text_match(recent_query, record.content, self.settings)
                if recent_query
                else TextMatch(0.0, False, False, False)
            )
            match = TextMatch(
                relevance=max(current_match.relevance, recent_match.relevance * 0.25),
                exact_phrase=current_match.exact_phrase,
                substring=current_match.substring or recent_match.substring,
                prefix=current_match.prefix or recent_match.prefix,
            )
            priority_score = round(record.priority / 100, 6)
            recency_score = _recency_score(record.updated_at, reference)
            final_score = round(
                match.relevance * self.settings.relevance_weight
                + priority_score * self.settings.priority_weight
                + recency_score * self.settings.recency_weight,
                6,
            )
            explicit = record.id in explicit_set
            reason: MemoryDecisionReason = "selected"
            included = True
            if record.superseded_by is not None:
                included, reason = False, "superseded"
            elif not record.active and not explicit and not self.settings.include_inactive:
                included, reason = False, "inactive"
            elif not explicit and match.relevance < self.settings.minimum_relevance:
                included, reason = False, "low_relevance"
            elif explicit:
                reason = "explicitly_attached"
            item = MemoryContextItem(
                memory_id=record.id,
                summary=safe_memory_summary(record.content),
                category=record.category,
                priority=record.priority,
                relevance_score=match.relevance,
                priority_score=priority_score,
                recency_score=recency_score,
                final_score=final_score,
                included=included,
                reason=reason,
            )
            if included:
                ranked.append(_RankedCandidate(record, item, explicit))
            elif include_debug_metadata:
                rejected.append(item)

        missing_explicit = [memory_id for memory_id in explicit_ids if memory_id not in records_by_id]
        if include_debug_metadata:
            rejected.extend(
                MemoryContextItem(
                    memory_id=memory_id,
                    summary="[deleted or unavailable memory]",
                    category="other",
                    priority=0,
                    relevance_score=0,
                    priority_score=0,
                    recency_score=0,
                    final_score=0,
                    included=False,
                    reason="deleted",
                )
                for memory_id in missing_explicit
            )

        ranked.sort(
            key=lambda candidate: (
                candidate.explicit,
                candidate.item.final_score,
                candidate.item.relevance_score,
                candidate.record.updated_at,
                candidate.record.id,
            ),
            reverse=True,
        )
        conflict_winners: dict[str, _RankedCandidate] = {}
        for candidate in ranked:
            key = memory_conflict_key(candidate.record.content)
            if key is None:
                continue
            current = conflict_winners.get(key)
            if current is None or self._conflict_rank(candidate) > self._conflict_rank(
                current
            ):
                conflict_winners[key] = candidate

        selected: list[MemoryContextItem] = []
        selected_normalized: set[str] = set()
        for candidate in ranked:
            conflict_key = memory_conflict_key(candidate.record.content)
            conflict_winner = (
                conflict_winners.get(conflict_key) if conflict_key is not None else None
            )
            if conflict_winner is not None and conflict_winner.record.id != candidate.record.id:
                if include_debug_metadata:
                    rejected.append(
                        candidate.item.model_copy(
                            update={"included": False, "reason": "conflict_lost"}
                        )
                    )
                continue
            normalized_content = normalize_memory_content(candidate.record.content)
            if normalized_content in selected_normalized:
                if include_debug_metadata:
                    rejected.append(candidate.item.model_copy(update={"included": False, "reason": "duplicate"}))
                continue
            if len(selected) >= top_k:
                if include_debug_metadata:
                    rejected.append(
                        candidate.item.model_copy(update={"included": False, "reason": "top_k_limit"})
                    )
                continue
            selected.append(candidate.item)
            selected_normalized.add(normalized_content)

        rejected.sort(
            key=lambda item: (item.final_score, item.relevance_score, item.priority, item.memory_id),
            reverse=True,
        )
        return MemoryRetrievalResult(
            backend=self.backend,
            top_k=top_k,
            candidate_count=len(records_by_id) + len(missing_explicit),
            selected=tuple(selected),
            rejected=tuple(rejected),
        )

    @staticmethod
    def _conflict_rank(candidate: _RankedCandidate) -> tuple[object, ...]:
        """Resolve known conflicts without allowing priority to decide alone."""

        return (
            candidate.explicit,
            candidate.record.updated_at,
            candidate.item.relevance_score,
            candidate.item.priority_score,
            candidate.record.id,
        )
