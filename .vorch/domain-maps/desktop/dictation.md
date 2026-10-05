# Desktop dictation

Read for work on Desktop dictation (`desktop/dictation/`): the stored `dictation` settings, the toggle and hold shortcut, Escape, the take flow and its pieces, the audio cues, pasting through the clipboard and its fallbacks, the status and failure codes, and the Live hold during dictation. The shared microphone setting, capture, and speech server client are in `desktop/voice.md` (Capture and microphones, Server client); the shortcut registration is in `desktop/live-voice.md` -> Hands-free starts; the Desktop root map is `desktop.md`.

## Ownership

- `desktop/dictation/controller.py` - `DictationController`, the single owner: the preference, its global shortcut (a `HotkeyController` over `DICTATION_HOTKEY`), the running take, status, and failures.
- `desktop/dictation/pieces.py` - `PieceCutter`: splits a long take into pieces at speech pauses (Take flow).
- `desktop/dictation/insertion.py` - `ClipboardTextInserter` (`TextInserter` protocol): pastes the text or leaves it in the clipboard; the Win32 calls sit behind `WindowsHost` (`_Win32Host`).
- `desktop/dictation/cues.py` - `CuePlayer`: rendered sine-tone cues played in order on `vbot-dictation-cues` (Windows `winsound`; silent elsewhere).
- Shared: `desktop/speech/microphone.py` (`MicrophoneService.create_capture`, `prepare_echo`), `desktop/speech/server_client.py` (`SpeechServerClient`), `desktop/hotkey.py` (`HotkeyController`, Escape arming), `desktop/page_events.py` (`publish_dictation`), `desktop/system_actions.py` (clipboard text).
- `desktop/main.py` builds the controller after Voice with the window's server URL, follows server changes (`set_server_url`), starts it after the Live voice shortcut in the `shown` callback, stops it after that shortcut on exit, and counts a running take as busy for the update restart.

## Stored settings

Desktop settings section `dictation` (`desktop/settings.py`):

```json
"dictation": {
  "hotkey": { "enabled": false, "ctrl": true, "alt": true, "shift": false, "win": false, "key": "KeyD" },
  "mode": "toggle"
}
```

- `hotkey` follows the Live voice shortcut rules (allowed keys, modifier rule, physical key registration, `desktop/live-voice.md` -> Hands-free starts); default disabled, Ctrl+Alt+D. Missing or malformed fields fall back per field to the defaults.
- `mode`: `toggle` (a press starts, the next press ends) or `hold` (records while the combination is held; letting go of any of its keys ends it). Anything else reads as `toggle`.

## Bridge and status

- Capability `dictation`: the global shortcut can be registered (Windows only); gates the Settings section.
- `getDictation()` / `setDictation(changes)` return `{supported, enabled, hotkey: {ctrl, alt, shift, win, key}, mode, error_code, state, last_failure}`. Without a dictation controller both reject.
- `setDictation` takes a partial change of `enabled`, `ctrl`, `alt`, `shift`, `win`, `key`, and `mode`. A non-object or an invalid `mode` returns `dictation_config_invalid`, an invalid combination `hotkey_invalid`; neither persists anything. A valid shortcut change persists and re-registers (`error_code` `hotkey_in_use` / `hotkey_failed` as for the Live voice shortcut); a mode change alone keeps the registration. Enabling the shortcut starts creating the echo stage when echo cancellation is on, so the first take does not wait for it.
- `state`: `idle`, `recording` (the take runs until its end is requested), `transcribing`.
- `last_failure`: `null` or `{code, at}` (`at` ISO 8601 UTC, seconds) of the latest take that inserted nothing although it was not cancelled; a later successful take keeps it. Codes: `server_unreachable` (also without a connected server), `speech_to_text_unconfigured`, `speech_to_text_unavailable`, `transcription_failed` and the other speech server codes, `microphone_unavailable`, `nothing_heard` (empty transcript), `inserted_to_clipboard` (the text was not pasted, see Insertion), `insert_failed` (the clipboard could not be written), `dictation_failed` (unexpected error).

## Take flow

- One take at a time, on its own thread `vbot-dictation`. A press while no take runs begins one: it records the window that has the focus now (the paste target) and the current server URL and mode. In `toggle` mode the next press requests the end; in `hold` mode the release does (the hotkey thread polls the held keys). A press while a take transcribes changes nothing.
- While a take runs, Escape is registered as a second global hotkey and cancels the take, also while it transcribes; other apps do not receive Escape during that time. It is released when the take ends.
- The take opens its own capture of the shared microphone (`create_capture`; shared mode, so it runs beside a Voice listener) and checks the server in parallel on `vbot-dictation-prepare`: speech readiness (unreachable, unconfigured, or unavailable speech-to-text fails the take right away), transcription warm-up, and the upload budget.
- The start cue plays once the microphone delivers audio; audio from the first `START_SKIP_SECONDS` (start cue plus 0.1 s) is discarded so the cue is not transcribed. An end request before the microphone delivers audio (a tap) cancels the take with the cancel cue.
- A take has no length limit (user requirement: a recording the user started ends only when the user ends it). After the end request recording continues for `TAIL_SECONDS` (0.2 s) so the last word is not clipped. A take keeps its audio when the microphone fails midway. A take shorter than `MIN_RECORDING_SECONDS` (0.3 s, an accidental tap) is dropped with the cancel cue.
- Pieces (`PieceCutter`): one upload has the server's limit, so a take is split. Once a piece holds `PIECE_TARGET_SECONDS` (90 s), it ends at the next pause (0.3 s below twice the piece's noise floor, a low percentile of its block RMS, at least an absolute floor for digital silence); without a pause within `PIECE_SEARCH_SECONDS` (30 s), or before the next block would exceed the upload budget, it ends at the quietest block of the quietest pause-long run; a recording rate change ends it at once. Every block lands in exactly one piece. Each finished piece goes as WAV to `POST /api/speech/transcribe` of the server the take started with, in order, on `vbot-dictation-transcribe` while the user keeps speaking; the first piece waits for the server check. A piece that fails ends the take at once with its code and the error cue, so the user does not keep talking into nothing.
- When the take ends, the stop cue plays, the rest of the audio goes up as the last piece, and the stripped texts of all pieces, joined with single spaces (empty ones skipped), are inserted once. Cancel, `stop()`, and Escape cancel requests in flight; a cancelled take inserts nothing.
- `stop()` (exit) unregisters the shortcut, cancels a running take, and is final.

## Insertion

`ClipboardTextInserter.insert(text, target_window)`:

- Reads the current clipboard text, writes the transcript as clipboard text, then pastes with a synthetic Ctrl+V (`SendInput`) only when the target still has the focus, the target is reachable (not elevated while the Desktop is not; an unknown elevation counts as elevated), and every modifier is released within `MODIFIER_RELEASE_TIMEOUT_SECONDS` (2 s; Ctrl+V must not become another shortcut). Otherwise the text stays in the clipboard (`inserted_to_clipboard`).
- After a paste the previous clipboard text returns after `CLIPBOARD_RESTORE_DELAY_SECONDS` (0.8 s, timer `vbot-dictation-clipboard`) unless the clipboard changed meanwhile (`GetClipboardSequenceNumber`). Clipboard content other than text is not restored.
- The raw transcript is inserted as returned (stripped); there is no post-processing.

## Cues

`start` (rising two tones), `stop` (falling two tones), `cancel` (one low tone), `error` (two low tones), rendered once into in-memory WAVs and played in request order; a failing playback is logged and skipped. A take that ends without inserting plays `error` (also for `inserted_to_clipboard`), a cancel plays `cancel`.

## Live hold

From the start of a take until its recording ends, the controller publishes `vbot-desktop-dictation` `{recording: true}`, then `{recording: false}` (again when the take ends, so a missed push cannot leave the call held), so the page holds a running Live voice call and the assistant does not hear or talk over the dictation. The WebUI (`app/desktop.svelte.js`, `followDesktopDictation`) holds one `claimMicrophone()` claim (`lib/microphoneUse.js`) while `recording` is true, which makes `LiveVoice.svelte` hold the call with reason `recording` like a page recording does (`model_tasks/live.md`); the claim ends with `recording: false` or when the page tears down. A page loaded while a take records holds nothing until the next push.

## WebUI

- `webui/src/lib/desktopBridge.js`: `getDesktopDictation`, `setDesktopDictation` (normalized answers; a non-object answer throws), `onDesktopDictationRecording`.
- `webui/src/components/settings/DesktopDictationSettings.svelte` (Settings -> Voice -> Dictation, `desktop_dictation`, `webui/settings.md`): enable, key combination (`ShortcutCombination.svelte`), mode, error banners, and the latest failure ("Last problem"); it re-reads the status every second until the state is `idle`, because the push reports only `recording`.

## Tests

- `tests/desktop/dictation/test_controller.py` (toggle and hold takes, tap, Escape while recording and transcribing, failure codes and cues, takes that cannot run, long takes in pieces: cut at pauses, without a pause, at the upload limit, no audio lost, joined text, a failing piece ending the take, server follow, settings, stop), `test_insertion.py` (paste and clipboard restore, every fallback to the clipboard, unwritable clipboard), `test_cues.py` (rendering, ordered playback on its thread).
- Shared: `tests/desktop/hotkey_test_support.py` (`FakeHotkeyApi`), `tests/desktop/test_hotkey.py` (hold release, Escape arming), `test_page_events.py` (dictation push), `test_bridge.py` (dictation methods and capability), `test_main.py` (wiring, startup without the audio stack).
- WebUI: `webui/src/components/__tests__/DesktopDictationSettings.test.js`, `webui/src/lib/__tests__/desktopBridge.test.js` (dictation wrappers and push), `webui/src/__tests__/App.desktop-voice.test.js` (the microphone counts as in use while a dictation records).
