"""Installation-private Model availability reported by credentialed OpenCode catalogs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import cast

from core.providers.accounts import Account
from core.utils.atomic import atomic_write_text
from core.utils.logging import get_logger

OAUTH_CATALOG_GRANT = "opencode_catalog_grant"
WORKSPACE_CATALOG_PROVIDERS = frozenset({"opencode-go", "opencode-zen"})
_LOGGER = get_logger("providers.catalogs")


def credential_fingerprint(credential: str) -> str:
    return hashlib.sha256(credential.encode()).hexdigest()


@dataclass(frozen=True)
class ModelAccountRoutes:
    """A Connection's current Accounts in credential order, or an unscoped route."""

    connection_id: str
    accounts: tuple[tuple[str, frozenset[str] | None], ...] | None = None

    def allowed_accounts(self, model_id: str) -> list[str] | None:
        if self.accounts is None:
            return None
        confirmed = [
            account for account, ids in self.accounts if ids is not None and model_id in ids
        ]
        return confirmed + [account for account, ids in self.accounts if ids is None]

    def __call__(self, model_id: str) -> str | None:
        eligible = self.allowed_accounts(model_id)
        if eligible is None:
            return self.connection_id
        if not eligible:
            return None
        provider_id, local_connection_id, *_ = self.connection_id.split(":")
        return Account(provider_id, local_connection_id, eligible[0]).connection_id


def account_model_routes(
    credentials: object, provider_id: str, connection_id: str
) -> ModelAccountRoutes:
    """Resolve Models on an already usable Connection, honoring exact Account pins."""
    resolve = getattr(credentials, "account_model_routes", None)
    if provider_id in WORKSPACE_CATALOG_PROVIDERS and resolve is not None:
        return cast(ModelAccountRoutes, resolve(provider_id, connection_id))
    return ModelAccountRoutes(connection_id)


class AccountCatalogs:
    """Last successful catalog per Account, bound to the credential used on the wire."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = Lock()
        self._entries: dict[Account, tuple[str, frozenset[str]] | None] = {}

    def _path(self, account: Account) -> Path:
        return (
            self.directory
            / account.provider_id
            / account.local_connection_id
            / f"{account.account_id}.json"
        )

    def get(self, account: Account, fingerprint: str) -> frozenset[str] | None:
        with self._lock:
            if account not in self._entries:
                entry = None
                try:
                    data = json.loads(self._path(account).read_text(encoding="utf-8"))
                    if isinstance(data, dict) and isinstance(data.get("credential"), str):
                        ids = data.get("models")
                        if isinstance(ids, list) and all(isinstance(item, str) for item in ids):
                            entry = (data["credential"], frozenset(ids))
                except FileNotFoundError:
                    pass
                except (ValueError, OSError) as exc:
                    _LOGGER.warning(
                        "Could not read Model access for %s: %s", account.connection_id, exc
                    )
                self._entries[account] = entry
            cached = self._entries[account]
            return cached[1] if cached is not None and cached[0] == fingerprint else None

    def replace(self, account: Account, fingerprint: str, model_ids: frozenset[str]) -> bool:
        if self.get(account, fingerprint) == model_ids:
            return False
        with self._lock:
            path = self._path(account)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                path,
                json.dumps({"credential": fingerprint, "models": sorted(model_ids)}),
                owner_only=True,
            )
            self._entries[account] = (fingerprint, model_ids)
        return True
