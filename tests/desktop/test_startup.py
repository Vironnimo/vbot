"""Fresh-process checks keep the optional audio stack out of Desktop startup."""

import subprocess
import sys
from pathlib import Path


def test_disabled_voice_builds_its_bridge_without_loading_the_audio_stack(tmp_path):
    script = """
import sys
from pathlib import Path
from desktop.bridge import DesktopBridge
from desktop.connection import ConnectionController
from desktop.main import _create_voice, parse_args
from desktop.page_events import PageEventDispatcher

settings = Path(sys.argv[1]) / 'settings.json'
controller = ConnectionController(settings_file=settings)
page_events = PageEventDispatcher()
voice = _create_voice(parse_args([]), settings, '', page_events)
bridge = DesktopBridge(voice=voice, connection=controller)
voice.start()
assert bridge.getDesktopCapabilities()['voiceApi'] == 2
assert bridge.getVoiceStatus()['state'] == 'off'
voice.close()
page_events.close()
audio_modules = {
    'desktop.wakeword.capture',
    'desktop.wakeword.detection',
    'desktop.wakeword.commands',
    'desktop.wakeword.echo',
    'desktop.wakeword._speech_detection',
}
assert not audio_modules & sys.modules.keys(), audio_modules & sys.modules.keys()
heavy = {'numpy', 'sounddevice', 'pyopen_wakeword', 'onnxruntime', 'soxr', 'livekit'}
assert not heavy & sys.modules.keys(), heavy & sys.modules.keys()

# The package still exposes Voice when a caller actually needs it.
from desktop.wakeword import VoiceController
assert VoiceController.__module__ == 'desktop.wakeword.controller'
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
