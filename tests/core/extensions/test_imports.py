"""The public facade keeps light consumers isolated and exported identities stable."""

import subprocess
import sys


def test_extension_facade_loads_exports_only_on_first_access() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib
import sys
import core.extensions as extensions
from core.extensions import oauth_redirects
assert "core.extensions.extensions" not in sys.modules
assert "core.extensions._loading" not in sys.modules
assert "core.extensions.operations" not in sys.modules
assert oauth_redirects is importlib.import_module("core.extensions.oauth_redirects")
for name in extensions.__all__:
    value = getattr(extensions, name)
    assert getattr(extensions, name) is value
assert extensions.ExtensionRegistry is importlib.import_module(
    "core.extensions.extensions"
).ExtensionRegistry
try:
    extensions.unrecognized_export
except AttributeError:
    pass
else:
    raise AssertionError("unknown exports must raise AttributeError")
""",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
