"""Fresh-process checks keep the optional audio worker out of Desktop startup."""

import subprocess
import sys
from pathlib import Path


def test_disabled_voice_builds_its_bridge_without_loading_the_audio_worker(tmp_path):
    script = """
import sys
from pathlib import Path
from desktop.connection import ConnectionController
from desktop.main import _create_wakeword_bridge, parse_args

settings = Path(sys.argv[1]) / 'settings.json'
controller = ConnectionController(settings_file=settings)
bridge = _create_wakeword_bridge(parse_args([]), settings, controller, '')
assert bridge.getWakewordStatus()['state'] == 'off'
bridge._stop_worker()
assert 'desktop.wakeword.worker' not in sys.modules
assert 'desktop.wakeword._audio_capture' not in sys.modules
assert not {'numpy', 'sounddevice', 'pyopen_wakeword', 'onnxruntime', 'soxr'} & sys.modules.keys()

# The package still exposes its Worker API when a caller actually needs it.
from desktop.wakeword import WakewordWorker
assert WakewordWorker.__module__ == 'desktop.wakeword.worker'
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
