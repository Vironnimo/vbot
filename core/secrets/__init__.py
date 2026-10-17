"""The Secret Store: credentials and OAuth tokens encrypted at rest.

Owns ``<data-dir>/secrets.json``, the protection of its data key, and the
conversion of the plaintext credential files it replaced (``.env`` and
``oauth/*.json``). Consumers keep resolving credentials by environment-variable
name; Storage's credential functions and the Provider token store build on it.
"""

from core.secrets._keys import (
    DPAPI_PROTECTION,
    KEY_FILE_ENVIRONMENT_VARIABLE,
    KEY_FILE_PROTECTION,
    CreatedKey,
    DpapiKeyProtection,
    KeyFileProtection,
    KeyProtection,
    KeyUnavailableError,
    default_key_protections,
)
from core.secrets.plaintext import (
    CONVERT_COMMAND,
    PLAINTEXT_CREDENTIAL_PATTERNS,
    PlaintextImport,
    PlaintextRetirement,
    ignored_plaintext_files,
    import_plaintext_credentials,
    plaintext_credential_files,
    plaintext_credential_reports,
    retire_plaintext_credentials,
    secret_store_health,
    settle_plaintext_credentials,
)
from core.secrets.store import (
    CREDENTIALS,
    ENTRY_NAME_PATTERN,
    OAUTH_TOKENS,
    SECRET_STORE_FILE_NAME,
    SECRET_STORE_FORMAT,
    SecretEntryUnavailableError,
    SecretStore,
    SecretStoreError,
    SecretStoreReset,
    SecretStoreStatus,
    SecretStoreUnavailableError,
    SecretTransaction,
    validate_secret_store_data,
    validate_secret_store_file,
)

__all__ = [
    "CONVERT_COMMAND",
    "CREDENTIALS",
    "DPAPI_PROTECTION",
    "ENTRY_NAME_PATTERN",
    "KEY_FILE_ENVIRONMENT_VARIABLE",
    "KEY_FILE_PROTECTION",
    "OAUTH_TOKENS",
    "PLAINTEXT_CREDENTIAL_PATTERNS",
    "SECRET_STORE_FILE_NAME",
    "SECRET_STORE_FORMAT",
    "CreatedKey",
    "DpapiKeyProtection",
    "KeyFileProtection",
    "KeyProtection",
    "KeyUnavailableError",
    "PlaintextImport",
    "PlaintextRetirement",
    "SecretEntryUnavailableError",
    "SecretStore",
    "SecretStoreError",
    "SecretStoreReset",
    "SecretStoreStatus",
    "SecretStoreUnavailableError",
    "SecretTransaction",
    "default_key_protections",
    "ignored_plaintext_files",
    "import_plaintext_credentials",
    "plaintext_credential_files",
    "plaintext_credential_reports",
    "retire_plaintext_credentials",
    "secret_store_health",
    "settle_plaintext_credentials",
    "validate_secret_store_data",
    "validate_secret_store_file",
]
