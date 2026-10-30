"""Lossless text pages and stateless continuations for calendar reads."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from itertools import chain
from typing import Any

from core.tools._calendar_arguments import CalendarCallRefusedError

PAGE_MAX_CHARACTERS = 30_000
# Headers, the continuation token and its ready-to-send call are budgeted below.
type Position = tuple[int, str, str]
type Record = tuple[Position, str]


def catalog_identity(events: Iterable[Any], jobs: dict[str, str], timezone: str) -> str:
    digest = hashlib.sha256()
    for value in chain((timezone, jobs), (event.to_dict() for event in events)):
        digest.update(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())
    return digest.hexdigest()


@dataclass
class CalendarPage:
    arguments: dict[str, Any]
    now: datetime
    identity: str
    position: Position = (0, "", "")
    offset: int = 0

    @classmethod
    def resume(cls, arguments: dict[str, Any], now: datetime, identity: str) -> CalendarPage:
        token = arguments.get("cursor")
        if token is None:
            return cls(dict(arguments), now, identity)
        try:
            payload = json.loads(base64.urlsafe_b64decode(token.encode()))
            request = payload["request"]
            position = payload["position"]
            offset = payload["offset"]
            if (
                payload["version"] != 1
                or not isinstance(payload.get("identity"), str)
                or not isinstance(request, dict)
                or request.get("action") != arguments["action"]
                or set(request) - {"action", "id", "query", "duration", "time_min", "time_max"}
                or not all(
                    isinstance(value, str) for key, value in request.items() if key != "duration"
                )
                or not isinstance(position, list)
                or len(position) != 3
                or type(position[0]) is not int
                or position[0] < 0
                or not all(isinstance(value, str) for value in position[1:])
                or type(offset) is not int
                or offset < 0
            ):
                raise ValueError
            frozen_now = datetime.fromisoformat(payload["now"])
            if frozen_now.tzinfo is None:
                raise ValueError
        except (ValueError, TypeError, KeyError, UnicodeError) as error:
            raise CalendarCallRefusedError(
                "calendar was not run: cursor is invalid for this action. "
                "Start the calendar query again without cursor."
            ) from error
        if payload["identity"] != identity:
            raise CalendarCallRefusedError(
                "calendar was not run: the calendar, its time zone or its bound jobs changed "
                "since the previous page. Start the query again without cursor."
            )
        extras = {
            key: value for key, value in arguments.items() if key in {"id", "query", "duration"}
        }
        # Repeated identical filters are harmless; changed filters start a new query.
        if any(request.get(key) != value for key, value in extras.items()):
            raise CalendarCallRefusedError(
                "calendar was not run: cursor already retains the query's window and filters. "
                "Continue with only action and cursor, or start a new query without cursor."
            )
        return cls(request, frozen_now, identity, (position[0], position[1], position[2]), offset)

    def _token(self, position: Position, offset: int) -> str:
        payload = {
            "version": 1,
            "request": self.arguments,
            "now": self.now.isoformat(),
            "identity": self.identity,
            "position": position,
            "offset": offset,
        }
        return base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        ).decode()

    def read(
        self,
        records: Iterator[Record],
        data: dict[str, Any],
        *,
        visible_length: Callable[[dict[str, Any]], int],
    ) -> dict[str, Any]:
        """Consume only this page and one lookahead record, including oversized records."""
        content = ""
        first = True
        returned = 0
        data = {**data, "complete": False}
        for position, text in records:
            offset = self.offset if first else 0
            first = False
            remaining = text[offset:]
            token = self._token(position, offset)
            continuation = {
                "next_cursor": token,
                "note": "More results follow. Continue with "
                + json.dumps(
                    {"action": self.arguments["action"], "cursor": token},
                    separators=(",", ":"),
                )
                + ".",
            }
            # Reserve the actual continuation, whose size depends on the query.
            available = (
                PAGE_MAX_CHARACTERS
                - visible_length({**data, **continuation, "content": content})
                - 32
            )
            if (
                len(remaining) > available
                or visible_length({**data, **continuation, "content": content + remaining})
                > PAGE_MAX_CHARACTERS - 32
            ):
                # Prefer record boundaries; split a record only if it cannot fit on a fresh page.
                take = max(0, available) if not content else 0
                if take:
                    # Neutralized reminder tags can expand when sent to the Model.
                    while (
                        visible_length(
                            {**data, **continuation, "content": content + remaining[:take]}
                        )
                        > PAGE_MAX_CHARACTERS
                    ):
                        take //= 2
                    content += remaining[:take]
                    returned += 1
                if not content:
                    raise CalendarCallRefusedError(
                        "calendar was not run: the continuation cannot fit in one output. "
                        "Use a shorter query."
                    )
                token = self._token(position, offset + take)
                continuation["next_cursor"] = token
                continuation["note"] = (
                    "More results follow. Continue with "
                    + json.dumps(
                        {"action": self.arguments["action"], "cursor": token}, separators=(",", ":")
                    )
                    + "."
                )
                if "free" in data:
                    data["free"] = returned
                return {**data, **continuation, "content": content}
            content += remaining
            returned += 1
        if "free" in data:
            data["free"] = returned
        return {**data, "complete": True, **({"content": content} if content else {})}
