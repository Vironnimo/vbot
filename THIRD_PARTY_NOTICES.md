# Third-Party Notices

## Local speech models

Optional local speech recognition downloads unmodified pretrained model weights
at first use; vBot does not bundle them in its distribution:

- Qwen Team, [Qwen3-ASR-1.7B-hf](https://huggingface.co/Qwen/Qwen3-ASR-1.7B-hf)
  and [Qwen3-ASR-0.6B-hf](https://huggingface.co/Qwen/Qwen3-ASR-0.6B-hf),
  under [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0).
- NVIDIA, [Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3),
  under [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/).
- NVIDIA, [Nemotron 3.5 ASR Streaming 0.6B](https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b),
  under OpenMDW-1.1 (see the model card's linked license terms).

The linked model cards provide upstream attribution and model documentation.

Optional local TTS downloads [Qwen3-TTS CustomVoice](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice)
weights (Apache-2.0) and [Chatterbox Multilingual V3](https://huggingface.co/ResembleAI/chatterbox)
weights (MIT). Their separately installed SDKs are [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)
(Apache-2.0) and [Chatterbox](https://github.com/resemble-ai/chatterbox) (MIT).
Chatterbox's upstream PerTh watermarking remains enabled. The setup bootstrap
[uv](https://github.com/astral-sh/uv) is available under Apache-2.0 or MIT;
managed Python distributions are supplied by Astral's python-build-standalone.
Weights and SDK source are downloaded on demand, not bundled in vBot.

## Silero VAD model

vBot includes `desktop/wakeword/models/silero_vad.onnx` from [Silero VAD](https://github.com/snakers4/silero-vad), copyright 2020-present Silero Team. The file comes from the `silero-vad` 6.2.1 PyPI package (`silero_vad/data/silero_vad.onnx`); the bundled file has SHA-256 `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3` and is distributed under the MIT License:

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Hey Nabu v2 wake-word model

vBot includes `desktop/wakeword/models/hey_nabu_v2.tflite` from the [Home Assistant Wake Words Collection](https://github.com/fwartner/home-assistant-wakewords-collection/tree/main/en/hey_nabu), copyright 2023 Florian Wartner. The bundled file has SHA-256 `ce18b69e1bddfb56e70fe739d6ca0f423f70a6e710f05b376baf6a3625689234` and is distributed under the MIT License:

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## pyopen-wakeword audio test fixtures

`tests/fixtures/wakeword/okay_nabu.wav` and `tests/fixtures/wakeword/unrelated_hey_jarvis.wav` come from the corresponding test directories in [rhasspy/pyopen-wakeword](https://github.com/rhasspy/pyopen-wakeword/tree/main/tests) and are distributed under that project's Apache License 2.0. Their SHA-256 values are `c19747e603b00db74eb53ee2a65ae0489dd9feb574a9e98683fc3be8740b6c66` and `05bf58195bd9c6af46becd373565c87bf0e57d133a950f2c0aa441daf1acb908`, respectively.

`tests/fixtures/wakeword/hey_nabu.wav` is a generated test fixture containing the spoken phrase "Hey Naboo", synthesized with the Microsoft Zira Desktop voice at rate -2. Its SHA-256 is `2bc6ddba7c57e6451de96d621bba95479b24085677f58fc933861e102e966679`.

## Wakeword audio dependencies

The Desktop wakeword pipeline uses [python-soxr](https://github.com/dofuuz/python-soxr), distributed under the GNU Lesser General Public License v2.1 or later (following its underlying libsoxr), for anti-aliased streaming resampling of native microphone rates to the detector's 16 kHz contract. It remains a separately installed dependency; its source distribution and license text are available from the linked upstream project and installed package metadata.

## Interactive terminal dependencies

The `terminal` Tool uses [pyte](https://github.com/selectel/pyte), distributed under the GNU Lesser General Public License v3, as an in-memory VT terminal emulator. It uses [pywinpty](https://github.com/andfoy/pywinpty), distributed under the MIT License, for ConPTY access on Windows and [ptyprocess](https://github.com/pexpect/ptyprocess), distributed under the ISC License, for PTY process control on POSIX systems. The WebUI Terminals surface uses [xterm.js](https://github.com/xtermjs/xterm.js), including `@xterm/xterm` and `@xterm/addon-fit`, distributed under the MIT License, for browser-side VT rendering and responsive fitting. These libraries remain separately installed dependencies; their source distributions and license texts are available from the linked upstream projects and installed package metadata.


## MCP client

The bundled MCP Extension uses the official [Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk), distributed under the MIT License. It remains a separately installed dependency; its source distribution and license text are available from the upstream project and installed package metadata.

## Playwright CLI Skill

`resources/skills/playwright-cli/` includes Microsoft's Playwright CLI Skill and
nine reference files from [microsoft/playwright-cli](https://github.com/microsoft/playwright-cli),
revision `655530f6d0dc71a0d6bf46ae165877d3c7311099` (CLI 0.1.19), under Apache-2.0.
The upstream license is included in that directory. `SKILL.md` adds a vBot
execution/session/file-handling introduction; reference files are unmodified.
`UPSTREAM.json` records the original hashes and attribution. The CLI executable
and browser binaries are installed separately and are not bundled with vBot.
