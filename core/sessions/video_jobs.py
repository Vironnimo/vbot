"""Generation-bound collection receipts for accepted Video jobs."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.runs.run import RunExecutionOwner
from core.sessions._types import SessionAddress
from core.sessions.errors import SessionNotFoundError

if TYPE_CHECKING:
    from core.sessions.store import SessionStore


@dataclass(frozen=True)
class VideoJob:
    id: str
    job_id: str
    target: str
    origin: str
    directory: str
    original_root: str
    generation: str
    address: SessionAddress
    run_id: str
    state: str
    media_type: str | None
    digest: str | None
    owner: RunExecutionOwner | None


class SessionVideoJobs:
    def __init__(self, store: SessionStore) -> None:
        self._store = store

    def record(
        self,
        identifier: str,
        generation: str,
        run_id: str,
        call_id: str,
        job_id: str,
        target: str,
        origin: str,
        directory: str,
        original_root: str,
    ) -> None:
        def insert(connection: sqlite3.Connection) -> None:
            added = connection.execute(
                """INSERT INTO session_video_jobs
                       (id,run_key,invocation_key,job_id,target,origin,directory,original_root)
                   SELECT ?,r.run_key,i.invocation_key,?,?,?,?,?
                   FROM runs r JOIN sessions s USING(session_key)
                       JOIN tool_invocations i USING(run_key)
                   WHERE s.generation_id=? AND s.state='live' AND r.run_id=?
                     AND r.inherited=0 AND r.status='running' AND i.name='generate_video'
                     AND i.call_id=? AND i.status='running'
                   ORDER BY i.invocation_key DESC LIMIT 1""",
                (
                    identifier,
                    job_id,
                    target,
                    origin,
                    directory,
                    original_root,
                    generation,
                    run_id,
                    call_id,
                ),
            )
            if not added.rowcount:
                raise SessionNotFoundError("Original video Run is unavailable")

        self._store._execute_write(insert)

    def pending(self, identifier: str | None = None) -> list[VideoJob]:
        def read(connection: sqlite3.Connection) -> list[VideoJob]:
            rows = connection.execute(
                """SELECT j.id,j.job_id,j.target,j.origin,j.directory,j.original_root,j.media_type,
                      j.digest,s.generation_id,s.project_id,s.agent_id,s.session_id,r.run_id,
                      o.owner_name, o.group_id, o.participant_id,
                      o.participant_generation_id, o.epoch,
                      CASE
                        WHEN s.state!='live' OR (r.status='cancelled'
                            AND coalesce(r.completion_reason,'')
                                NOT IN ('shutdown','update','process_restart')) THEN 'stopped'
                        WHEN j.delivered=1 OR e.superseded_at_seq IS NOT NULL
                          OR (i.status='cancelled' AND coalesce(r.completion_reason,'')
                                     NOT IN ('shutdown','update','process_restart'))
                          OR (i.result_ok=1 AND CASE WHEN json_valid(t.content)
                                  THEN json_type(t.content,'$.data.video.path')='text' END)
                            THEN 'finished'
                        WHEN r.status='running' AND i.result_ok=1 AND
                             CASE WHEN json_valid(t.content)
                               THEN json_extract(t.content,'$.data.status')='pending' END
                             THEN 'pending'
                        ELSE r.status END AS job_state
                    FROM session_video_jobs j JOIN runs r USING(run_key)
                      JOIN sessions s USING(session_key)
                      JOIN tool_invocations i ON i.invocation_key=j.invocation_key
                      LEFT JOIN entries e ON e.entry_key=i.result_entry_key
                      LEFT JOIN entry_text t ON t.entry_key=i.result_entry_key
                      LEFT JOIN run_execution_owners o USING(run_key)"""
                + (" WHERE j.id=?" if identifier is not None else ""),
                (identifier,) if identifier is not None else (),
            )
            return [
                VideoJob(
                    row["id"],
                    row["job_id"],
                    row["target"],
                    row["origin"],
                    row["directory"],
                    row["original_root"],
                    row["generation_id"],
                    SessionAddress(row["project_id"] or None, row["agent_id"], row["session_id"]),
                    row["run_id"],
                    row["job_state"],
                    row["media_type"],
                    row["digest"],
                    RunExecutionOwner(
                        row["owner_name"],
                        row["group_id"],
                        row["participant_id"],
                        row["participant_generation_id"],
                        row["epoch"],
                    )
                    if row["owner_name"]
                    else None,
                )
                for row in rows
            ]

        return self._store._read(read)

    def ready(self, identifier: str, media_type: str, digest: str) -> None:
        self._store._execute_write(
            lambda c: (
                c.execute(
                    "UPDATE session_video_jobs SET media_type=?,digest=? WHERE id=?",
                    (media_type, digest, identifier),
                ).rowcount
            )
        )

    def forget(self, identifier: str) -> None:
        self._store._execute_write(
            lambda c: c.execute("DELETE FROM session_video_jobs WHERE id=?", (identifier,)).rowcount
        )
