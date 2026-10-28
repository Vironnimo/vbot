"""Channel configuration schema and validated values."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.channels._choices import (
    ALLOWED_CHANNEL_DM_SCOPES,
    ALLOWED_CHANNEL_PLATFORMS,
    ALLOWED_CHANNEL_RESPONSE_MODES,
    ALLOWED_EMAIL_SMTP_SECURITY,
)
from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonObject,
    JsonValidationReport,
    add_error,
    error_diagnostic,
    load_validated_json_file,
    validate_allowed_string,
    validate_json_file,
    validate_non_empty_string,
    warn_unknown_keys,
)
from core.json_documents import (
    FORMAT_VERSION_FIELD,
    JsonDocumentFormat,
    json_document,
    strip_unknown_fields,
    validate_format_version,
)
from core.settings import is_valid_agent_id
from core.utils.errors import VBotError

if TYPE_CHECKING:
    pass

_DEFAULT_DM_SCOPE = "per_conversation"


_DEFAULT_RESPONSE_MODE = "mention"


MANAGED_CHANNEL_TOKEN_ENV_PREFIX = "VBOT_CHANNEL_TOKEN__"


_CHANNEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


_MUTABLE_FIELDS = frozenset(
    (
        "platform",
        "agent_id",
        "dm_scope",
        "allowed_chat_ids",
        "token_env_var",
        "enabled",
        "response_mode",
        "mention_patterns",
        "observe_unaddressed",
        "app_token_env_var",
        "server_url",
        "email",
    )
)


_EMAIL_FIELDS = frozenset(
    (
        "address",
        "username",
        "imap_host",
        "imap_port",
        "smtp_host",
        "smtp_port",
        "smtp_security",
        "folders",
        "require_sender_authentication",
    )
)
_DEFAULT_IMAP_PORT = 993
_DEFAULT_SMTP_PORT = 465
_DEFAULT_EMAIL_FOLDERS = ("INBOX",)
# A practical address check, not RFC 5322: one @, no whitespace, no characters
# that would break a header or an address list.
_EMAIL_ADDRESS_PATTERN = re.compile(r"^[^@\s<>()\[\],;:\"\\]+@[^@\s<>()\[\],;:\"\\]+$")
_HOST_PATTERN = re.compile(r"^[A-Za-z0-9.:-]+$")


_CHANNEL_CONFIG_FIELDS = _MUTABLE_FIELDS | {"id"}


CHANNEL_FORMAT_VERSION = 1


CHANNEL_SHAPE = json_document(_CHANNEL_CONFIG_FIELDS)


class ChannelError(VBotError):
    """Base class for expected channel-domain errors.

    ``retryable`` marks transient transport failures (network blips, platform
    rate limits) that reply delivery may retry; permanent platform rejections
    stay non-retryable. ``retry_after`` carries a rate-limit wait hint honored
    as a floor by the retry loop. ``possibly_delivered`` marks a failed send
    that may nonetheless have shown the message, in full or in part: an earlier
    part was acknowledged, or the platform's answer to a write never arrived.
    """

    def __init__(
        self,
        message: str = "",
        *,
        retryable: bool = False,
        retry_after: float | None = None,
        possibly_delivered: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.possibly_delivered = possibly_delivered


class ChannelNotFoundError(ChannelError):
    """Raised when a channel id is unknown."""


class ChannelConfigError(ChannelError):
    """Raised when channel config data is invalid."""


def validate_channel_file(config_path: str | Path) -> JsonValidationReport:
    """Validate one persisted ``channel.json`` without consuming it."""
    return validate_json_file(config_path, validate_channel_document, missing_ok=False)


def load_validated_channel_json(config_path: str | Path) -> JsonObject:
    """Load the Channel config fields of one schema-valid ``channel.json``.

    Unknown fields are left out; the Channel writer merges them back from disk.
    """
    try:
        data = load_validated_json_file(config_path, validate_channel_document, missing_ok=False)
    except JsonConfigValidationError as error:
        raise ChannelConfigError(str(error)) from error
    fields = cast("JsonObject", strip_unknown_fields(data, CHANNEL_SHAPE))
    fields.pop(FORMAT_VERSION_FIELD, None)
    return fields


def validate_channel_document(data: Any) -> list[JsonDiagnostic]:
    """Validate a decoded raw ``channel.json`` document, including its version."""
    if not isinstance(data, dict):
        return [error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]
    diagnostics: list[JsonDiagnostic] = []
    if not validate_format_version(diagnostics, data, CHANNEL_FORMAT_VERSION):
        return diagnostics
    fields = {key: value for key, value in data.items() if key != FORMAT_VERSION_FIELD}
    return diagnostics + validate_channel_data(fields)


CHANNEL_FORMAT = JsonDocumentFormat(
    name="Channel config",
    version=CHANNEL_FORMAT_VERSION,
    shape=CHANNEL_SHAPE,
    validate=validate_channel_document,
    sort_keys=True,
)


def validate_channel_data(data: Any) -> list[JsonDiagnostic]:
    """Validate the Channel config fields of a decoded mapping.

    ``format_version`` belongs to the file; see :func:`validate_channel_document`.
    """
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, dict):
        return [error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]

    warn_unknown_keys(diagnostics, "$", data, _CHANNEL_CONFIG_FIELDS, "channel field")
    try:
        _normalize_channel_id(data.get("id"))
    except ChannelConfigError as error:
        add_error(diagnostics, "$.id", str(error))
    validate_allowed_string(
        diagnostics, "$.platform", data.get("platform"), ALLOWED_CHANNEL_PLATFORMS
    )
    _validate_channel_agent_id(diagnostics, "$.agent_id", data.get("agent_id"))
    validate_allowed_string(
        diagnostics,
        "$.dm_scope",
        data.get("dm_scope", _DEFAULT_DM_SCOPE),
        ALLOWED_CHANNEL_DM_SCOPES,
    )
    _validate_platform_id_list(diagnostics, "$.allowed_chat_ids", data.get("allowed_chat_ids", []))
    if data.get("platform") != "whatsapp":
        validate_non_empty_string(
            diagnostics, "$.token_env_var", data.get("token_env_var"), required=True
        )
    for key in ("token_env_var", "app_token_env_var", "server_url"):
        if key in data and not isinstance(data[key], str):
            add_error(diagnostics, f"$.{key}", "must be a string")
    if data.get("platform") == "slack":
        validate_non_empty_string(
            diagnostics, "$.app_token_env_var", data.get("app_token_env_var"), required=True
        )
        if (
            str(data.get("app_token_env_var", "")).strip()
            == str(data.get("token_env_var", "")).strip()
        ):
            add_error(diagnostics, "$.app_token_env_var", "must differ from token_env_var")
    if data.get("platform") == "mattermost":
        from urllib.parse import urlsplit

        try:
            url = urlsplit(data.get("server_url", ""))
            valid = (
                url.scheme in {"http", "https"}
                and bool(url.hostname)
                and not (url.username or url.password or url.query or url.fragment)
            )
        except ValueError, TypeError, AttributeError:
            valid = False
        if not valid:
            add_error(
                diagnostics,
                "$.server_url",
                "must be an HTTP(S) server URL without credentials, query or fragment",
            )
    if data.get("platform") == "whatsapp":
        if data.get("allowed_chat_ids", []) not in ([], ["self"]):
            add_error(
                diagnostics,
                "$.allowed_chat_ids",
                "WhatsApp supports only the self chat: use ['self'] or []",
            )
        if data.get("token_env_var") or data.get("app_token_env_var") or data.get("server_url"):
            add_error(
                diagnostics, "$", "WhatsApp uses linked-device pairing, not tokens or a server URL"
            )
    if data.get("platform") == "email":
        _validate_email_settings(diagnostics, "$.email", data.get("email"))
        for index, chat_id in enumerate(data.get("allowed_chat_ids", [])):
            if isinstance(chat_id, str) and normalize_email_address(chat_id) is None:
                add_error(diagnostics, f"$.allowed_chat_ids[{index}]", "must be an email address")
        if data.get("app_token_env_var") or data.get("server_url"):
            add_error(diagnostics, "$", "Email uses the email settings, not an app token or URL")
    elif data.get("email") is not None:
        add_error(diagnostics, "$.email", "is only valid for the email platform")
    if "enabled" in data and not isinstance(data["enabled"], bool):
        add_error(diagnostics, "$.enabled", "must be a boolean")
    if "observe_unaddressed" in data and not isinstance(data["observe_unaddressed"], bool):
        add_error(diagnostics, "$.observe_unaddressed", "must be a boolean")
    validate_allowed_string(
        diagnostics,
        "$.response_mode",
        data.get("response_mode", _DEFAULT_RESPONSE_MODE),
        ALLOWED_CHANNEL_RESPONSE_MODES,
    )
    _validate_regex_list(diagnostics, "$.mention_patterns", data.get("mention_patterns", []))
    return diagnostics


def normalize_email_address(value: Any) -> str | None:
    """Return the trimmed, lowercased address, or None when it is not an email address."""
    if not isinstance(value, str):
        return None
    address = value.strip().lower()
    if len(address) > 254 or _EMAIL_ADDRESS_PATTERN.fullmatch(address) is None:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in address):
        return None
    return address


def _validate_email_settings(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if not isinstance(value, dict):
        add_error(diagnostics, path, "must be an object with the email account settings")
        return
    warn_unknown_keys(diagnostics, path, value, _EMAIL_FIELDS, "email field")
    if normalize_email_address(value.get("address")) is None:
        add_error(diagnostics, f"{path}.address", "must be an email address")
    if "username" in value and (
        not isinstance(value["username"], str) or not value["username"].strip()
    ):
        add_error(diagnostics, f"{path}.username", "must be a non-empty string")
    for host_key, port_key in (("imap_host", "imap_port"), ("smtp_host", "smtp_port")):
        host = value.get(host_key)
        if not isinstance(host, str) or _HOST_PATTERN.fullmatch(host.strip()) is None:
            add_error(diagnostics, f"{path}.{host_key}", "must be a host name or IP address")
        port = value.get(port_key)
        if port is not None and (
            isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
        ):
            add_error(diagnostics, f"{path}.{port_key}", "must be a port number from 1 to 65535")
    if "smtp_security" in value:
        validate_allowed_string(
            diagnostics,
            f"{path}.smtp_security",
            value["smtp_security"],
            ALLOWED_EMAIL_SMTP_SECURITY,
        )
    folders = value.get("folders", list(_DEFAULT_EMAIL_FOLDERS))
    if (
        not isinstance(folders, list)
        or not folders
        or not all(isinstance(folder, str) and folder.strip() for folder in folders)
    ):
        add_error(diagnostics, f"{path}.folders", "must be a non-empty list of folder names")
    elif any(any(ord(character) < 32 for character in folder) for folder in folders):
        add_error(diagnostics, f"{path}.folders", "must not contain control characters")
    if "require_sender_authentication" in value and not isinstance(
        value["require_sender_authentication"], bool
    ):
        add_error(diagnostics, f"{path}.require_sender_authentication", "must be a boolean")


@dataclass(frozen=True, slots=True)
class EmailSettings:
    """The mail account of an email Channel; its password is the Channel credential."""

    address: str
    imap_host: str
    smtp_host: str
    username: str = ""
    imap_port: int = _DEFAULT_IMAP_PORT
    smtp_port: int = _DEFAULT_SMTP_PORT
    smtp_security: str = ""
    folders: tuple[str, ...] = _DEFAULT_EMAIL_FOLDERS
    require_sender_authentication: bool = True

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EmailSettings:
        """Build normalized settings from schema-valid data, filling the defaults."""
        address = normalize_email_address(payload.get("address")) or ""
        smtp_port = int(payload.get("smtp_port") or _DEFAULT_SMTP_PORT)
        return cls(
            address=address,
            username=str(payload.get("username") or address).strip(),
            imap_host=str(payload.get("imap_host", "")).strip(),
            imap_port=int(payload.get("imap_port") or _DEFAULT_IMAP_PORT),
            smtp_host=str(payload.get("smtp_host", "")).strip(),
            smtp_port=smtp_port,
            smtp_security=str(
                payload.get("smtp_security") or ("ssl" if smtp_port == 465 else "starttls")
            ),
            folders=tuple(
                folder.strip() for folder in payload.get("folders") or _DEFAULT_EMAIL_FOLDERS
            ),
            require_sender_authentication=bool(payload.get("require_sender_authentication", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "username": self.username,
            "imap_host": self.imap_host,
            "imap_port": self.imap_port,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "smtp_security": self.smtp_security,
            "folders": list(self.folders),
            "require_sender_authentication": self.require_sender_authentication,
        }


def _email_payload(value: EmailSettings | Mapping[str, Any] | None) -> Any:
    return value.to_dict() if isinstance(value, EmailSettings) else value


def _validate_channel_agent_id(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        add_error(diagnostics, path, "must be a non-empty string")
    elif not is_valid_agent_id(value.strip()):
        add_error(
            diagnostics,
            path,
            "must be 1-64 characters using only letters, numbers, hyphen, or underscore",
        )


def _validate_platform_id_list(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if not isinstance(value, list):
        add_error(diagnostics, path, "must be a list of platform ids")
        return
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            add_error(diagnostics, f"{path}[{index}]", "must be a string or integer id")
        elif isinstance(item, str) and not item.strip():
            add_error(diagnostics, f"{path}[{index}]", "must not be empty")


def _validate_regex_list(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if not isinstance(value, list):
        add_error(diagnostics, path, "must be a list of regex strings")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            add_error(diagnostics, f"{path}[{index}]", "must be a non-empty string")
            continue
        try:
            re.compile(item)
        except re.error as error:
            add_error(diagnostics, f"{path}[{index}]", f"must be a valid regex: {error}")


@dataclass(slots=True)
class ChannelConfig:
    """Persisted channel configuration."""

    id: str
    platform: str
    agent_id: str
    dm_scope: str = _DEFAULT_DM_SCOPE
    allowed_chat_ids: list[str] = field(default_factory=list)
    token_env_var: str = ""
    enabled: bool = True
    response_mode: str = _DEFAULT_RESPONSE_MODE
    mention_patterns: list[str] = field(default_factory=list)
    observe_unaddressed: bool = False
    app_token_env_var: str = ""
    server_url: str = ""
    # The mail account of an email Channel; None for every other platform.
    email: EmailSettings | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize one channel config to JSON-compatible data."""
        payload = {
            "id": self.id,
            "platform": self.platform,
            "agent_id": self.agent_id,
            "dm_scope": self.dm_scope,
            "allowed_chat_ids": list(self.allowed_chat_ids),
            "token_env_var": self.token_env_var,
            "enabled": self.enabled,
            "response_mode": self.response_mode,
            "mention_patterns": list(self.mention_patterns),
            "observe_unaddressed": self.observe_unaddressed,
            "app_token_env_var": self.app_token_env_var,
            "server_url": self.server_url,
        }
        if self.email is not None:
            payload["email"] = _email_payload(self.email)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ChannelConfig:
        """Create one ChannelConfig from persisted JSON data."""
        _raise_channel_config_errors(payload)
        config = cls(
            id=payload.get("id", ""),
            platform=payload.get("platform", ""),
            agent_id=payload.get("agent_id", ""),
            dm_scope=payload.get("dm_scope", _DEFAULT_DM_SCOPE),
            allowed_chat_ids=list(payload.get("allowed_chat_ids") or []),
            token_env_var=payload.get("token_env_var", ""),
            enabled=payload.get("enabled", True),
            response_mode=payload.get("response_mode", _DEFAULT_RESPONSE_MODE),
            mention_patterns=list(payload.get("mention_patterns") or []),
            observe_unaddressed=payload.get("observe_unaddressed", False),
            app_token_env_var=payload.get("app_token_env_var", ""),
            server_url=payload.get("server_url", ""),
            email=(
                EmailSettings.from_dict(payload["email"])
                if isinstance(payload.get("email"), Mapping)
                else None
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        """Validate with the persisted schema, then normalize accepted values."""
        _raise_channel_config_errors(
            {
                "id": self.id,
                "platform": self.platform,
                "agent_id": self.agent_id,
                "dm_scope": self.dm_scope,
                "allowed_chat_ids": self.allowed_chat_ids,
                "token_env_var": self.token_env_var,
                "enabled": self.enabled,
                "response_mode": self.response_mode,
                "mention_patterns": self.mention_patterns,
                "observe_unaddressed": self.observe_unaddressed,
                "app_token_env_var": self.app_token_env_var,
                "server_url": self.server_url,
                **({} if self.email is None else {"email": _email_payload(self.email)}),
            }
        )
        if isinstance(self.email, Mapping):
            # Accessors hand the settings in as their JSON object.
            self.email = EmailSettings.from_dict(self.email)
        if self.platform == "email":
            self.allowed_chat_ids = [
                normalize_email_address(value) or str(value) for value in self.allowed_chat_ids
            ]
        self.id = self.id.strip()
        self.agent_id = self.agent_id.strip()
        self.token_env_var = self.token_env_var.strip()
        self.app_token_env_var = self.app_token_env_var.strip()
        self.server_url = self.server_url.strip().rstrip("/")
        self.allowed_chat_ids = [str(value).strip() for value in self.allowed_chat_ids]
        self.mention_patterns = list(self.mention_patterns)


def _raise_channel_config_errors(payload: Any) -> None:
    errors = [
        diagnostic
        for diagnostic in validate_channel_data(payload)
        if diagnostic.severity == "error"
    ]
    if errors:
        raise ChannelConfigError("; ".join(f"{error.path}: {error.message}" for error in errors))


def _normalize_channel_id(channel_id: Any) -> str:
    if not isinstance(channel_id, str) or not channel_id.strip():
        raise ChannelConfigError("channel_id must be a non-empty string")
    normalized = channel_id.strip()
    # The id becomes a path segment under the channels directory, and delete recursively
    # removes that directory: a separator or traversal component (``../agents``, ``/etc``)
    # would let an operation escape storage and rmtree an arbitrary directory. Enforce the
    # same bare-slug rule ChannelConfig already requires, at the choke point every storage
    # and service call funnels through.
    if _CHANNEL_ID_PATTERN.fullmatch(normalized) is None:
        raise ChannelConfigError(
            "channel_id must contain only letters, numbers, underscore, and hyphen"
        )
    return normalized


def managed_channel_token_env_var(channel_id: str) -> str:
    """Return a collision-free, environment-safe key for a managed Channel token."""
    normalized_id = _normalize_channel_id(channel_id)
    encoded_id = normalized_id.encode("utf-8").hex().upper()
    return f"{MANAGED_CHANNEL_TOKEN_ENV_PREFIX}{encoded_id}"
