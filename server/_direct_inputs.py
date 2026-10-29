"""Secret-authenticated task submission and scoped durable status reads."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.automation.inputs import INPUT_PATH, DirectInputError, DirectInputNotFoundError
from core.secrets import SecretStoreError
from core.sessions.errors import AccessorInputConflictError, SessionNotFoundError
from core.utils.errors import VBotError
from core.utils.ids import new_id
from server._http_dependencies import FastAPIType, HTTPException, Request, Response
from server._request_bodies import _read_json_payload_with_limit
from server.access import AccessService
from server.access._service import WEBHOOK_REFUSED


def _response(status: int, payload: dict[str, Any]) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=True),
        status_code=status,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def _error(status: int, code: str, message: str) -> Response:
    return _response(status, {"ok": False, "error": {"code": code, "message": message}})


def register_input_routes(app: FastAPIType, access: AccessService) -> None:
    async def handle(request: Request, input_id: str, request_id: str | None = None) -> Response:
        facts = access.inspect(request.scope)
        path = str(request.scope["path"])
        retry = access.admit_webhook(facts, path)
        if retry is not None:
            response = _error(429, "rate_limited", "Too many requests")
            response.headers["Retry-After"] = str(max(1, round(retry)))
            return response
        inputs = request.app.state.runtime.direct_inputs
        values = request.headers.getlist("authorization")
        submitting = request_id is None
        try:
            if len(values) != 1 or not await inputs.authenticate(
                input_id, values[0], submitting=submitting
            ):
                access.refused_webhook(WEBHOOK_REFUSED, facts, path)
                return _error(404, "not_found", "Not found")
            if not submitting:
                return _response(200, {"ok": True, **await inputs.result(input_id, request_id)})
            body = await _read_json_payload_with_limit(request)
            if not isinstance(body, dict):
                raise DirectInputError("Request body must be a JSON object")
            wait = body.get("wait", False)
            timeout = body.get("wait_timeout_seconds", 30)
            if (
                not isinstance(wait, bool)
                or isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not 0 < timeout <= 60
            ):
                raise DirectInputError(
                    "wait must be a boolean and wait_timeout_seconds must be between 0 and 60"
                )
            keys = request.headers.getlist("idempotency-key")
            if len(keys) > 1 or (keys and (not keys[0].strip() or len(keys[0]) > 128)):
                raise DirectInputError(
                    "Idempotency-Key must be one non-empty value of at most 128 characters"
                )
            request_id = (
                "key_" + hashlib.sha256(keys[0].encode()).hexdigest() if keys else new_id("req")
            )
            async with request.app.state.agent_delete_lock:
                if not await inputs.authenticate(input_id, values[0], submitting=True):
                    access.refused_webhook(WEBHOOK_REFUSED, facts, path)
                    return _error(404, "not_found", "Not found")
                result = await inputs.submit(input_id, body, request_id=request_id)
            if wait:
                result = await inputs.wait(input_id, request_id, timeout)
            status = (
                200
                if result["status"]
                in {"completed", "failed", "cancelled", "interrupted", "unavailable", "removed"}
                else 202
            )
            response = _response(status, {"ok": True, **result})
            response.headers["Location"] = result["status_url"]
            return response
        except DirectInputNotFoundError, SessionNotFoundError:
            return _error(404, "not_found", "Not found")
        except AccessorInputConflictError:
            return _error(
                409, "input_conflict", "This Idempotency-Key already identifies a different request"
            )
        except SecretStoreError:
            return _error(503, "unavailable", "Task input credentials are unavailable")
        except HTTPException as error:
            return _error(error.status_code, "invalid_request", str(error.detail))
        except (VBotError, ValueError) as error:
            return _error(400, "invalid_request", str(error))

    @app.post(f"{INPUT_PATH}{{input_id}}", include_in_schema=False)
    async def submit(request: Request, input_id: str) -> Response:
        return await handle(request, input_id)

    @app.get(f"{INPUT_PATH}{{input_id}}/requests/{{request_id}}", include_in_schema=False)
    async def result(request: Request, input_id: str, request_id: str) -> Response:
        return await handle(request, input_id, request_id)
