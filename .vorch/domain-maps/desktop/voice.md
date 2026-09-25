# Desktop Voice

Read for work on Desktop Voice (`desktop/wakeword/`): the listener pipeline, the stored `wakeword` settings, the Voice bridge methods, the status snapshot and pushed events, command recording and sending, microphones, echo cancellation, wake phrase models, and calibration. The root map `desktop.md` holds the ownership routing, threads, and invariants every Desktop task needs. Live voice starts and the Live hold are in `desktop/live-voice.md`; the WebUI Voice owner and panel are in `webui/settings.md`. Paths below are relative to `desktop/wakeword/` unless they start with `desktop/`, `tests/`, or `webui/`.

## Pipeline

One listener generation (a `_Session` in `controller.py`) owns one capture, one detection loop, one recorder, one command pipeline, and one `VoiceServerClient`, all sharing one stop event:

```
AudioCapture (vbot-voice-capture)
  native 40 ms reads -> mono int16 -> echo stage -> soxr to 16 kHz -> AudioBlock
  -> CaptureSubscription (bounded, never blocks capture)
       -> DetectionLoop (vbot-voice-detection): 80 ms chunks, SpeechGate, engine
            -> VoiceController._on_detection
                 -> live_voice action: page_events.request_live(mode, "wakeword")
                 -> command action: CommandRecorder (vbot-voice-recorder), own subscription
                      -> CommandPipeline (vbot-voice-command-1..3): transcribe, resolve Session, send
```

Other threads: `vbot-voice-start` / `vbot-voice-stop` (transitions), `vbot-voice-readiness`, `vbot-voice-mock`, `vbot-voice-echo-init` (echo stage creation), `vbot-echo-reference` (echo reference monitor), and `vbot-desktop-page-events` (push delivery, `desktop/page_events.py`).

## Lifecycle

- `VoiceController.start()` runs in the window's `shown` callback and builds a listener only when Voice is enabled; `close()` runs once in `desktop/main.py`'s `finally` and joins with bounded timeouts.
- Every rebuild goes through `_transition_locked`: it bumps the generation, signals the old session, resets runtime state (a recording in progress ends with `recording_ended` for the page), and runs the rest on a `vbot-voice-start` / `vbot-voice-stop` thread: join the previous transition, `session.shutdown` (bounded joins; threads that do not stop in time are abandoned with a warning), then `_build_session`. Callbacks of an older generation are dropped.
- Triggers: enable/disable, a changed microphone, echo cancellation, active phrases or sensitivities, a different server URL (`set_server_url`; an unchanged URL is a no-op), and `retry()`. Routing changes (phrase actions, default Agent, default Session behavior) do not restart capture: they bump the routing version and re-run readiness.
- `retry()` rereads the stored config and forces a transition; in the unavailable mode it also re-probes the stack.
- `_build_session` order: resolve the mode (the stack probe `_real_wakeword_available` in `desktop/main.py` runs once, lazily, on the transition thread) -> unavailable fails `voice_stack_unavailable`, mock starts the mock session, no server fails `no_server` -> `echo_stages.prepare()` when echo cancellation is on -> create the engine (`WakewordModelError` fails with its code, anything else `engine_start_failed`) -> build capture, detection (subscription bound 2.0 s), recorder, and pipeline -> start capture and detection and request readiness. The capture, detection, command, speech-detection, and echo modules are first imported on this path, never at Desktop startup.
- Fatal listener failures (`engine_start_failed`, `detection_failed`, capture `failed` with `pipeline_failed`, an unexpected transition error) stop the session and hold state `error` until the next transition (a listener setting change, Retry, disable and enable, or a server switch). Routing changes do not clear it.

## Stored settings

`config.py` is the only owner of the `wakeword` section in `desktop/settings.py`; it reads with `read_section` and writes through `update_section` (one locked read-modify-write). Shape (every field optional):

```json
{
  "enabled": true,
  "microphone": { "index": 4, "name": "Studio mic", "host_api": "Windows WASAPI" },
  "echo_cancellation": true,
  "active_model_ids": ["builtin/hey_nabu", "builtin/hey_jarvis"],
  "model_sensitivities": { "builtin/hey_nabu": 0.55 },
  "server_profiles": {
    "http://127.0.0.1:8421": {
      "target_agent_id": "main",
      "session_behavior": "active",
      "phrase_actions": {
        "builtin/hey_jarvis": { "type": "command", "agent_id": "coder", "session_behavior": "new" },
        "builtin/okay_nabu": { "type": "live_voice", "mode": "toggle" }
      }
    }
  }
}
```

- Acoustic settings are global: `microphone` (stable identity or `null` for automatic), `echo_cancellation` (default `true`), `active_model_ids` (1..8 unique catalog ids, `MAX_ACTIVE_PHRASES`; default Okay Nabu + Hey Nabu), `model_sensitivities` (0.05..0.95, default 0.5, threshold = 1 - sensitivity; inactive models keep theirs). Sensitivity must stay strictly below 1.0: a zero threshold with `score >= threshold` would turn every zero score into a detection.
- Routing is per server profile, keyed by the canonical server base URL (`localhost` merges onto `127.0.0.1`), because Agent ids are server-specific: `target_agent_id` (default command Agent), `session_behavior` (`active` | `new`, default `active`), `phrase_actions` (entries of inactive phrases stay stored). A malformed action field drops that whole entry.
- Effective action (`VoiceConfig.effective_action`): no entry means a command with the profile defaults; a command entry's missing `agent_id` / `session_behavior` falls back to the profile's; the Agent stays `None` when neither is set. `live_voice` actions carry `mode` `start` | `toggle` (default `toggle`).
- `parse_voice_config` is tolerant: malformed entries fall back one by one; malformed `active_model_ids` select the defaults. `apply_voice_changes` is strict: unknown change keys or invalid values reject the whole change with `VoiceConfigError` (`voice_config_invalid`, naming the field); profile keys without a connected server reject with `no_server`; unknown model ids are rejected except in a `null` removal. Change keys: `microphone`, `echo_cancellation`, `active_model_ids`, `model_sensitivities` (merged), `default_agent_id` (`null` clears), `default_session_behavior`, `phrase_actions` (merged; `null` removes an entry).
- The unreleased global `model_actions` key is ignored on read and removed on the next write. Writers keep unknown keys.
- Deleting an imported model runs `forget_model`: its sensitivity and its action in every profile are removed; `active_model_ids` is untouched (an active model cannot be deleted).

## Bridge methods

`desktop/bridge.py` delegates every Voice method to `VoiceController`; the error contract is in `desktop.md` -> Python<->JS bridge. The WebUI enables Voice only for `voiceApi === 2`.

- `getVoiceStatus()` -> status snapshot.
- `setVoiceEnabled(enabled)` -> `{enabled, error_code}`; persists and applies. `error_code` is always `null`: readiness problems appear in the status, the switch is never refused.
- `updateVoiceConfig(changes)` -> status snapshot; validated, persisted, then applied.
- `listMicrophones()` -> `[{index, name, host_api, default_sample_rate, supported, capture_sample_rate}]` (shared-mode inputs only; empty without sounddevice).
- `listWakewordModels()` -> descriptors `{id, label, source, format, removable, kind, overlaps}`, built-ins first, then valid imports by label; never local paths or executable targets.
- `importWakewordModel(filename, content_base64)` -> descriptor plus `activated`. The bridge bounds the base64 length and decodes strictly; the model is activated only while fewer than 8 phrases are active.
- `deleteWakewordModel(model_id)` -> `{deleted: true}`; an active model rejects `wakeword_model_active`.
- `retryVoice()`, `stopVoiceRecording()` (ends the current recording and keeps its audio, so the command is still sent; no effect without a recording) -> status snapshot.
- `startVoiceCalibration(model_id)`, `restartVoiceCalibration()`, `stopVoiceCalibration()` -> status snapshot.

Error codes a Voice bridge call can reject with: `voice_config_invalid`, `no_server`, `wakeword_model_invalid`, `wakeword_model_unavailable`, `wakeword_model_active`, `wakeword_model_delete_failed`, `calibration_unavailable`, `calibration_inactive`.

## Status snapshot

`VoiceController.status()` / `getVoiceStatus()` and every `status` push carry:

- `enabled`, `mode` (`real` | `mock` | `unavailable`), `state`, `error_code`, `sequence`.
- `state` is derived, in order: `off` (disabled) -> `error` (fatal error set) -> `microphone_disconnected` (capture disconnected, `error_code` `microphone_unavailable` or `microphone_read_failed`) -> `listening` (listener started and capture capturing, or mock) -> `starting`. Recording and command progress are not states.
- `microphone` (stored selection), `active_microphone` (`{index, name, host_api, sample_rate}` of the open device, authoritative for automatic selection).
- `echo_cancellation`: `{enabled, state}`; `state` is `off` whenever no capture runs.
- `default_agent_id`, `default_session_behavior` of the current server's profile.
- `phrases`: one per active phrase, `{model_id, label, sensitivity, action, effective, problem}`; `action` is the stored action, `effective` the resolved one, `problem` the code that keeps a command phrase from working (see Readiness), `null` for Live voice actions.
- `recording`: `null` or `{command_id, model_id, agent_id}` while a command records (the Live hold follows it, `desktop/live-voice.md`).
- `commands`: `[{command_id, model_id, stage}]` with `stage` `transcribing` | `sending`.
- `calibration`: `null` or `{model_id, phase, score, peak, noise_level, noise_high, sample_count, required_samples, recommended_sensitivity, noise_seconds_remaining}`.
- `limits`: `{max_active_phrases, min_sensitivity, max_sensitivity}`.

## Events and push

- Event shape: `{sequence, kind, ...fields}` with `null` fields omitted. Status pushes and events share one `sequence` counter, incremented per publication, and are published under the controller lock so delivery order matches the sequence.
- Kinds: `detected` and `live_requested` (model_id); `recording_started` / `recording_ended` (command_id, model_id, agent_id); the command outcomes `no_speech`, `cancelled`, `transcription_failed` (error_code), `command_failed` (error_code, agent_id), and `sent` (agent_id, session_id), each with command_id and model_id; `command_failed` for a phrase problem has no command_id because nothing was recorded. `microphone_disconnected` (error_code), `microphone_reconnected`, and `error` (error_code) announce state transitions.
- Delivery: `desktop/page_events.py` dispatches `vbot-desktop-voice` with `detail` `{type: "status", status}` or `{type: "event", event}` (not cancelable). At most one status waits (a newer snapshot replaces it in place); at most 64 events wait and the oldest is dropped, which the page detects as a sequence gap and answers by re-reading the status. Calibration status pushes are throttled to one per 0.2 s.
- The page side (snapshot adoption, gap handling, cues, Toasts) is documented in `webui/settings.md`. Cues (`playVoiceCue` in `webui/src/lib/desktopBridge.js`) are best-effort and silently skipped when Web Audio cannot play; the visual state stays authoritative.

## Readiness and phrase problems

- A `vbot-voice-readiness` thread checks the server for the command phrases: `task_model.status` for `speech_to_text` (only when a command phrase is active), `agent.get` per distinct command Agent, and the upload budget. It repeats when the routing changed meanwhile and never blocks listening.
- Speech problem codes: `speech_to_text_unconfigured`, `speech_to_text_unavailable`, `speech_to_text_readiness_failed`, `server_unreachable`. Agent problems: `target_agent_unavailable` (any refusal of `agent.get`, including the RPC `agent_not_found`) or `server_unreachable`.
- A phrase's `problem` is `missing_target_agent` (no Agent resolved), else the speech problem, else its Agent's problem.
- A command detection with a problem emits `detected` then `command_failed` with the problem code and records nothing; it re-requests readiness unless the problem is `missing_target_agent`. Readiness older than 300 s is re-checked on the next command detection. A `target_agent_unavailable` send outcome marks that Agent's problem immediately.

## Detection

- `DetectionLoop` re-chunks the 16 kHz projection into 80 ms (1280-sample) chunks, starts the engine on its own thread, and hands every winner to `on_detection` with at least 0.32 s of pre-roll in whole capture blocks. `on_detection` runs synchronously and must return promptly.
- `SpeechGate` (`_speech_detection.py`, one per loop): a chunk's scores count only if any of the chunks 4 to 6 before it carried speech (upstream openWakeWord semantics: the heads peak after the phrase ended). It stays closed for the first 4 chunks after start or reset, is reset on every capture gap, and is open when no speech detector exists.
- Speech detection: `SpeechDetector` runs the bundled Silero VAD v5 ONNX model on strict 512-sample hops with a 64-sample context and hysteresis (opens at probability >= 0.5, closes below 0.35); every consumer creates its own because the model keeps stream state. Without onnxruntime, `create_fallback_vad` (webrtcvad mode 1) judges 10 ms slices and needs 2 speech slices. Every path fails open: a missing or failing detector counts audio as speech and never mutes Voice.
- `MultiWakewordEngine` (`engine.py`) runs every active head over one shared `pyopen_wakeword` feature stream. `speech_present=False` zeroes a chunk's scores before confirmation and before the score listener. A score >= threshold + 0.15 confirms at once; a marginal crossing must cross again within the last 5 chunks. Each phrase re-arms below its threshold independently; overlapping phrases (catalog `overlaps`) share one arm group. Among confirmations the highest score-to-threshold ratio wins, so independently tuned heads stay comparable.
- While a calibration runs, matches are suppressed and scores still reach the calibration. A command detection during a recording is ignored (logged); a Live voice detection is always forwarded.

## Commands

- `CommandRecorder` records one command at a time from its own subscription (bound 10 s), started right after the last pre-roll block (`capture.subscribe(after_index=...)`) and seeded with the pre-roll. Speech endpointing uses its own `SpeechDetector` on 32 ms hops: non-speech audio waits in a 0.4 s pre-speech buffer and is kept once speech starts; 1.0 s of silence after speech ends the recording; no speech within 1.5 s ends it as `no_speech`; reaching the upload budget ends it and keeps the audio below the budget; a user stop ends it and keeps the audio (`no_speech` when no speech started yet); a capture gap or a recording-rate change discards the recording (`command_failed`, `microphone_read_failed`); the stop event cancels it. There is no fixed duration cap.
- The WAV uses the capture's recording rate: 48 kHz while the echo stage is attached (states `active` and `no_reference`), else the native device rate (pass-through states `off`, `starting`, `unavailable`). The server converts it to the `speech.transcription_audio` profile (`model_tasks/speech.md`).
- `CommandPipeline` runs up to 3 workers: `transcribing` -> an empty transcript gives `no_speech` -> a transcript that equals or ends with a reserved cancel phrase (`abbrechen`, `vergiss es`, after normalization) gives `cancelled` -> `sending`: resolve the Session, then send. Outcomes become events: `sent`, `no_speech`, `cancelled`, `transcription_failed`, `command_failed` (unexpected errors: `pipeline_failed`). Once its stop event is set it publishes nothing more; `close` discards queued commands. A cancel phrase never cancels an already started Run.
- Logs record codes, ids, and Agent routing, never transcripts.

## Server client

- `server_client.py` (`VoiceServerClient`) uses `POST /api/rpc` and `POST /api/speech/transcribe` with `trust_env=False` and no keep-alive; the session's stop event cancels requests (`VoiceRequestCancelled`), and `close()` aborts requests in flight.
- Retries: idempotent reads (`agent.get`, `session.list`, `settings.get_path`, `task_model.status`) and transcription get up to 3 attempts on transport errors and HTTP 429/502/503/504; a transcription read timeout is not retried. `session.create` and `chat.stream` get exactly one attempt, because a retry after the server committed could duplicate a Session or Run. A 200 response with malformed JSON raises `VoiceServerInvalidResponse` with the operation's error code and is never replayed. Any RPC refusal with `agent_not_found` maps to `target_agent_unavailable`.
- Timeouts: RPC 10 s; transcription 600 s for local model download/loading and inference, with connect 10 s, write 30 s, pool 10 s.
- `resolve_session`: `new` -> `session.create` with `make_current: true`; `active` -> `agent.get` `current_session_id`, else the newest `session.list` entry excluding subagent, reflection, Cron, and Channel Sessions, else `session.create`.
- `send_command` calls `chat.stream` with `input_origin: "speech_transcription"`, so the Model receives the speech-origin context and Voice returns to listening once the server accepted the message.
- Upload budget: 0.9 x (`speech.upload_max_size_bytes` - 44 WAV header bytes), read once per client through `settings.get_path`; a failed or malformed read uses the 100 MiB default.
- Error codes: `server_unreachable`, `target_agent_unavailable`, `session_resolution_failed`, `send_failed`, `transcription_failed`.

## Capture and microphones

- Microphone identity (`_microphones.py`): the stored `{index, name, host_api}` index is used only while name and host API still match; otherwise a single exact name + host API match is used, else the device is unavailable, never a recycled index. `null` selects the system default input, then the host API defaults, then any usable input. Windows WDM-KS inputs are never listed or used: kernel streaming captures exclusively and would lock out other clients such as a Live voice call; WASAPI, MME, and DirectSound expose the same hardware.
- In the Voice panel the automatic option shows `active_microphone` (the device actually open); unsupported devices stay listed but disabled, and a stored device that is gone shows as unavailable.
- Capture format: the device's default rate first, then 48000/44100/32000/16000 Hz, minimum 16 kHz, `int16` or `float32`, mono.
- Every PortAudio query, open, close, and refresh holds `AUDIO_BACKEND_LOCK`; open streams are counted, and a PortAudio refresh (terminate/initialize) is skipped while one is open.
- `AudioBlock` carries `pcm16` (16 kHz, for detection, the speech gate, and endpointing, via a stateful soxr resampler) and `recording` (echo stage output at `recording_rate`, for the WAV). Blocks are numbered; 3 s of history lets a new subscription start right after a given block.
- Gaps (`CaptureGap.reason`): `overflow` (samples lost, reading continues), `read_failed` (stream reopened or disconnected), `echo_failed`, `echo_attached` (recording rate changed), `overrun` (this subscription fell behind), `history` (the requested start block is gone).
- Recovery: a failed read reopens the stream; 3 consecutive failures or a failed reopen disconnect the microphone (`microphone_read_failed`) and retry every 30 s with a PortAudio refresh first. A microphone that cannot open at start is refreshed and retried once, then disconnected (`microphone_unavailable`). An unexpected capture exception ends the capture as `failed` (`pipeline_failed`).

## Echo cancellation

- Purpose: remove what the speakers play (Live voice, Chat TTS, any other audio) from the microphone signal before any consumer sees it, so speaker output neither triggers phrases nor lands in command recordings.
- Engine: WebRTC AEC3 with a high-pass filter, through `livekit.rtc.AudioProcessingModule` (the `livekit` package's local native library; no LiveKit server or room is contacted). The stage runs at 48 kHz (`ECHO_SAMPLE_RATE`). The native side terminates the process on malformed input, so `echo.py` validates every frame first.
- Reference: the loopback of the default playback device, captured with PyAudioWPatch (WASAPI loopback; Windows only; its own PortAudio copy, independent of sounddevice). A `vbot-echo-reference` monitor opens it, polls the default playback endpoint about once a second, reopens it after a device change or a stopped stream, and retries a failed open after 30 s. WASAPI loopback delivers only while something plays; silence is treated as a silent reference.
- The stage may hold microphone audio back briefly while it waits for the matching reference and release it later; the hold is bounded. The alignment algorithm is internal to `echo.py`.
- States (`status.echo_cancellation.state`): `off` (disabled, or no capture), `starting` (the stage is still being created; audio passes through), `active` (reference open, silent, or being opened), `no_reference` (no loopback: other platforms, PyAudioWPatch missing, no playback device, open failure; the resampled microphone audio passes through), `unavailable` (the library cannot load or the stage failed; pass-through).
- `EchoStagePool` (`capture.py`, process-wide): `create_echo_stage` runs on `vbot-voice-echo-init` (a first import can take seconds). A capture waits at most 0.5 s for a stage, then listens in `starting` and attaches the stage at a block boundary (an `echo_attached` gap when the recording rate changes, which discards an in-flight recording). One stage is lent to one capture at a time and returned when the capture ends; while an abandoned capture still holds it, the next capture gets a new one. A factory that returns `None` or raises makes echo `unavailable` for the rest of the process. A stage whose `open` or `process` raises is closed, never returned to the pool, and the capture continues in `unavailable` (the next capture gets a fresh stage). The stage is closed before a PortAudio refresh.
- With echo cancellation disabled or unavailable, speaker output can still trigger phrases.

## Models and calibration

- Built-ins (`engine.py`): Okay Nabu, Hey Nabu (bundled MIT `models/hey_nabu_v2.tflite`), Hey Jarvis, Hey Mycroft, Hey Rhasspy, Alexa. Okay Nabu and Hey Nabu overlap. Model training is outside vBot.
- Imports: one finished `.tflite` up to 20 MiB, validated by constructing a detector before it becomes visible, stored as `<config-dir>/wakewords/<uuid>.tflite` plus `.json` metadata (`custom/<uuid>` ids). Deleting a built-in or a failed file removal rejects `wakeword_model_delete_failed`; an unknown id `wakeword_model_unavailable`; an invalid file `wakeword_model_invalid`.
- Calibration (`calibration.py`, `PhraseCalibration`) of one active phrase requires the real mode and state `listening` (else `calibration_unavailable`): 3 s of room noise (95th percentile of the phrase's scores), then 5 spoken samples (score peaks), then `ready` with a recommendation quantized to 0.05 between the noise level and the median peak. A run ends after 3 minutes; the recommendation is only reported, never stored. The page's Apply stops the calibration and saves the value as a sensitivity change, which restarts the listener.

## Mock and unavailable modes

- `--mock-wakeword` selects the mock mode: no audio, no network, and no server needed; the first active phrase is detected periodically and each detection walks `detected` -> `recording_started` -> `recording_ended` -> `transcribing` -> `sending` -> `sent`. It is the only path that simulates activity.
- Without the flag, a failed stack probe (pyopen_wakeword, sounddevice, soxr, webrtcvad) selects the unavailable mode: state `error` with `voice_stack_unavailable`, never simulated. onnxruntime and livekit are not part of the probe: without them speech detection falls back to webrtcvad and echo cancellation is `unavailable`.

## Tests

- `tests/desktop/test_controller.py` (lifecycle, dispatch, commands, status, and events end to end over fake devices and a fake server), `test_config.py`, `test_capture.py` (device selection, projections, gaps, recovery, echo stage pool), `test_echo.py`, `test_detection.py`, `test_speech_detection.py`, `test_commands.py`, `test_server_client.py`, `test_engine.py`, `test_engine_mock.py`, `test_engine_live_audio.py` (real audio, skipped without `pyopen_wakeword`), `test_calibration.py`, `test_bridge.py` (Voice methods and error contract), `test_page_events.py` (Voice pushes), `test_startup.py` (disabled Voice keeps the stack out of startup), `test_wakeword_settings.py` (`--mock-wakeword`). Shared doubles: `tests/desktop/voice_fakes.py`.
- WebUI: `webui/src/lib/__tests__/desktopBridge.test.js`, `wakewordSettings.test.js`, `webui/src/__tests__/App.test.wakeword.test.js`, `webui/src/components/__tests__/WakewordVoiceSettings.test.js`, `voiceLabels.test.js`, `AppShell.test.js` (sidebar Voice indicator).
