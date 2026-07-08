"""Make the scripts/ directory importable for the quality-runner tests.

``test_quality.py`` / ``test_quality_frontend.py`` load ``scripts/quality*.py``
via ``importlib.spec_from_file_location``, which does not put ``scripts/`` on
``sys.path``. Those runners import their shared sibling ``_quality_common``, so
add ``scripts/`` here. (Running the scripts directly already puts ``scripts/``
on ``sys.path[0]``, so this is only needed under the test loader.)
"""

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
