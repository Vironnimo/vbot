"""Synthetic tasks and canonical data; expectations never reach the Model."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.chat import ChatMessage
from core.chat.messages import ToolCall
from core.model_tasks import EmbeddingResult, EmbeddingSpaceIdentity
from core.runs import Run, RunKind
from core.sessions import ChatSessionManager, SessionAddress
from core.storage.layout import initialize_data_directory
from core.utils.ids import new_id


def recall_cases() -> list[dict[str, Any]]:
    return [
        {
            "id": "correction",
            "task": "Welche Aufbewahrungsdauer haben wir fuer Aurora beschlossen?",
            "contains": ["30"],
        },
        {
            "id": "scoped",
            "task": "Suche beim Agent coder nur in Session session-retention: Welche "
            "Aurora-Aufbewahrungsdauer gilt?",
            "contains": ["30"],
            "arguments": {"session_id": "session-retention"},
        },
        {
            "id": "empty_recovery",
            "task": "Welche Aufbewahrungsdauer haben wir fuer Aurora beschlossen?",
            "seed": {"query": "Aurora Aufbewahrung nonexistent"},
            "contains": ["30"],
        },
        {
            "id": "missing_query_recovery",
            "task": "Welche Aufbewahrungsdauer haben wir fuer Aurora beschlossen?",
            "seed": {},
            "contains": ["30"],
        },
        {
            "id": "unsupported_option_recovery",
            "task": "Welche Aufbewahrungsdauer haben wir fuer Aurora beschlossen?",
            "seed": {"query": "Aurora", "page": 2},
            "contains": ["30"],
        },
        {
            "id": "reversed_period",
            "task": "Welche Aurora-Aufbewahrungsdauer haben wir im Juli 2026 beschlossen?",
            "seed": {"query": "Aurora", "period": "2026-07-31/2026-07-01"},
            "contains": ["30"],
        },
        {
            "id": "current_session",
            "task": "Was hatten wir in diesem aktuellen Gespraech zur Rotation beschlossen?",
            "seed": {"query": "Rotation", "session_id": "current-session"},
            "no_more_search": True,
        },
        {
            "id": "exact_quote",
            "task": "Zitiere den letzten Satz der langen Assistant-Antwort zur "
            "Aurora-Freigabe in Session session-quote vollstaendig und "
            "wortgetreu.",
            "contains": ["Freigabe erst nach bestandenem Restore-Test."],
            "extended": True,
        },
        {
            "id": "tool_result",
            "task": "Welchen abschliessenden RESTORE_CHECK meldete das damalige Tool "
            "Result der Aurora-Diagnose in Session session-diagnostic?",
            "contains": ["OK-7319"],
            "extended": True,
        },
        {
            "id": "transcript",
            "task": "Lies alle User- und Assistant-Nachrichten aus Session "
            "session-transcript beim Agent coder. Schreibe sie vollstaendig "
            "und in Reihenfolge als JSONL nach transcript.jsonl im "
            "Arbeitsverzeichnis; andere Rollen bitte weglassen.",
            "extended": True,
            "transcript": True,
        },
        {
            "id": "delegated_period",
            "task": "Was ergab die delegierte Aurora-Pruefung beim Agent reviewer im "
            "Juli 2026? Beziehe nur diesen Zeitraum ein.",
            "contains": ["4817"],
            "arguments": {"agent_id": "reviewer", "include_subagents": True},
            "period": True,
        },
        {
            "id": "summary",
            "task": "Welchen exakten Wortlaut hatte meine damalige Zusage zu Borealis?",
            "contains": [],
            "summary": True,
        },
        {
            "id": "semantic_unavailable",
            "backend": "vector",
            "task": "Finde die damals beschlossene Aurora-Aufbewahrungsdauer. Aendere "
            "keine Einstellungen.",
            "contains": ["30"],
            "extended": True,
        },
        {
            "id": "hybrid_degraded",
            "backend": "hybrid",
            "task": "Welche Aufbewahrungsdauer haben wir fuer Aurora beschlossen?",
            "contains": ["30"],
        },
        {
            "id": "semantic",
            "backend": "vector",
            "embeddings": True,
            "task": "Wie lange sollten die Aurora-Daten erhalten bleiben?",
            "contains": ["30"],
        },
        {
            "id": "hybrid",
            "backend": "hybrid",
            "embeddings": True,
            "task": "Welche Aufbewahrungsdauer haben wir fuer Aurora beschlossen?",
            "contains": ["30"],
        },
        {
            "id": "narrow_results",
            "task": "Finde die beschlossene Helios-Kennung fuer Region Nord.",
            "seed": {"query": "Helios"},
            "contains": ["NORD-7284"],
        },
        {
            "id": "no_bash",
            "bash": False,
            "task": "Zitiere den letzten Satz der langen Assistant-Antwort zur "
            "Aurora-Freigabe in Session session-quote wortgetreu.",
            "unavailable": True,
        },
    ]


def recall_matrix() -> list[dict[str, Any]]:
    """Invocation conformance is separate from the natural workflow tasks."""
    shapes = {
        "query": {"query": "Aurora"},
        "period": {"query": "Aurora", "period": "2026-07-01/2026-07-31"},
        "open_start": {"query": "Aurora", "period": "/2026-07-31"},
        "open_end": {"query": "Aurora", "period": "2026-07-01/"},
        "agent": {"query": "Aurora", "agent_id": "reviewer"},
        "session": {"query": "Aurora", "session_id": "session-retention"},
        "subagents": {"query": "Aurora", "include_subagents": True},
        "exclude_subagents": {"query": "Aurora", "include_subagents": False},
        "all": {
            "query": "Aurora",
            "agent_id": "reviewer",
            "session_id": "session-delegated",
            "period": "2026-07-01T01:00:00+01:00/2026-07-31T23:59:59Z",
            "include_subagents": True,
        },
        "encoded_boolean": {"query": "Aurora", "include_subagents": "true"},
        "boolean_alias": {"query": "Aurora", "include_subagents": "yes"},
        "field_typo": {"qurey": "Aurora", "sessionId": "session-retention"},
        "search_wrapper": {"request": {"operation": "search", "query": "Aurora"}},
    }
    return [
        {
            "id": f"matrix_{backend}_{name}",
            "backend": backend,
            "embeddings": True,
            "task": "Call session_search exactly once with these arguments: "
            + json.dumps(arguments)
            + ". Do not repair or repeat the call. Report whether it succeeded.",
            "exact_arguments": arguments,
            "expected_ok": True,
        }
        for backend in ("sqlite_fts", "vector", "hybrid")
        for name, arguments in shapes.items()
    ]


async def seed_sessions(root: Path) -> ChatSessionManager:
    initialize_data_directory(root)
    sessions = ChatSessionManager(root)
    try:
        await _seed(sessions)
    except BaseException:
        # An open store would keep the fixture directory locked on Windows.
        sessions.close()
        raise
    return sessions


async def _admit_run(
    sessions: ChatSessionManager, address: SessionAddress, run_kind: RunKind
) -> None:
    """Admit one Run of *run_kind*; admission records the Session's Run kind."""
    await sessions.start_run(
        Run(
            run_id=new_id("run"),
            agent_id=address.agent_id,
            session_id=address.session_id,
            project_id=address.project_id,
            run_kind=run_kind,
        )
    )


async def _seed(sessions: ChatSessionManager) -> None:
    stamp = datetime(2026, 7, 12, 10, tzinfo=UTC)
    data = {
        "session-retention": [
            ChatMessage.user("Welche Aufbewahrungsdauer waehlen wir fuer Aurora?", timestamp=stamp),
            ChatMessage.assistant(
                model="fixture", content="Zunaechst 7 Tage fuer Aurora.", timestamp=stamp
            ),
            ChatMessage.assistant(
                model="fixture",
                content="Korrektur: Beschlossen sind 30 Tage Aufbewahrung fuer Aurora.",
                timestamp=stamp,
            ),
        ],
        "session-quote": [
            ChatMessage.user("Erklaere die Aurora-Freigabe ausfuehrlich.", timestamp=stamp),
            ChatMessage.assistant(
                model="fixture",
                content="Aurora-Freigabe: "
                + "Pruefschritte und technische Details. " * 160
                + "Freigabe erst nach bestandenem Restore-Test.",
                timestamp=stamp,
            ),
        ],
        "session-diagnostic": [
            ChatMessage.user("Fuehre die Aurora-Diagnose aus.", timestamp=stamp),
            ChatMessage.assistant(
                model="fixture",
                content=None,
                timestamp=stamp,
                tool_calls=[ToolCall(id="diagnostic", name="bash")],
            ),
            ChatMessage.tool(
                tool_call_id="diagnostic",
                name="bash",
                content="Diagnosedetails " * 600 + "\nRESTORE_CHECK=OK-7319",
                timestamp=stamp,
            ),
            ChatMessage.assistant(
                model="fixture", content="Die Diagnoseausgabe wurde gespeichert.", timestamp=stamp
            ),
        ],
        "session-transcript": [
            ChatMessage.user("Erste Frage: Welche Farbe?", timestamp=stamp),
            ChatMessage.assistant(model="fixture", content="Blau.", timestamp=stamp),
            ChatMessage.assistant(
                model="fixture",
                content=None,
                timestamp=stamp,
                tool_calls=[ToolCall(id="color", name="bash")],
            ),
            ChatMessage.tool(
                tool_call_id="color",
                name="bash",
                content="Excluded machine result",
                timestamp=stamp,
            ),
            ChatMessage.user("Zweite Frage: Welcher Farbton?", timestamp=stamp),
            ChatMessage.assistant(
                model="fixture", content="Kobaltblau, Farbcode #0047AB.", timestamp=stamp
            ),
        ],
        "session-summary": [
            ChatMessage.compaction_checkpoint(
                summary="Borealis: Der User stimmte dem Umzug zu. Der genaue Wortlaut ist "
                "nicht ueberliefert.",
                projection=[],
                compacted_token_count=10,
                timestamp=stamp,
            )
        ],
    }
    for name, messages in data.items():
        session = sessions.create("coder", session_id=name)
        session.append_many(messages)
        sessions.set_title(session.address, name.removeprefix("session-"))
        await _admit_run(sessions, session.address, RunKind.USER)
    sessions.create("coder", session_id="session-retention", project_id="other-project").append(
        ChatMessage.user("Aurora: 999 Tage")
    )
    for index in range(12):
        sessions.create("coder", session_id=f"helios-{index}").append(
            ChatMessage.user(f"Helios Kennung fuer Region {index}: H-{index}", timestamp=stamp)
        )
    sessions.create("coder", session_id="helios-north").append(
        ChatMessage.assistant(
            model="fixture",
            content="Helios Region Nord: beschlossen ist NORD-7284.",
            timestamp=stamp,
        )
    )
    sub = sessions.create("reviewer", session_id="session-delegated")
    sub.append(
        ChatMessage.assistant(
            model="fixture",
            content="Aurora-Pruefung abgeschlossen: Freigabecode 4817.",
            timestamp=stamp,
        )
    )
    await _admit_run(sessions, sub.address, RunKind.SUBAGENT)
    old = sessions.create("reviewer", session_id="session-old")
    old.append(
        ChatMessage.assistant(
            model="fixture", content="Aurora-Pruefung: Code 9999.", timestamp=stamp.replace(month=6)
        )
    )
    await _admit_run(sessions, old.address, RunKind.SUBAGENT)


class FixtureEmbeddings:
    """Deterministic topic vectors; evaluates workflow, not embedding quality."""

    def resolve_space(self) -> EmbeddingSpaceIdentity:
        return EmbeddingSpaceIdentity(
            provider_id="fixture", model_id="topics", fingerprint="recall-workflow-v1"
        )

    async def embed(self, texts: list[str], *, purpose: str | None = None) -> EmbeddingResult:
        vectors = []
        for text in texts:
            folded = text.casefold()
            vectors.append(
                [
                    float(any(term in folded for term in terms))
                    for terms in (
                        ("aurora",),
                        ("aufbewahr", "tage", "lange", "erhalten"),
                        ("freigabe", "restore"),
                        ("borealis",),
                        ("helios",),
                    )
                ]
                + [0.1]
            )
        return EmbeddingResult(
            vectors=tuple(vectors),
            model_id="topics",
            provider_id="fixture",
            dimension=6,
            space_fingerprint="recall-workflow-v1",
        )
