# Third-Party Notices

## File search engine

Server installations include the unmodified ripgrep 15.1.0 executable from
[BurntSushi/ripgrep](https://github.com/BurntSushi/ripgrep/releases/tag/15.1.0),
including PCRE2 10.45. Exact archives and executable digests are recorded in
`resources/ripgrep.lock.json`. The accompanying MIT and PCRE2 license notices
are distributed in `resources/licenses/ripgrep.txt` and `resources/licenses/pcre2.txt`.

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

## Echo cancellation dependencies

The Desktop echo cancellation uses the [LiveKit Python SDK](https://github.com/livekit/python-sdks) (`livekit`), copyright 2023 LiveKit, Inc., distributed under the Apache License 2.0, only for its local audio processing module; vBot contacts no LiveKit server or service through it. The package includes the native `livekit_ffi` library from [LiveKit's Rust SDKs](https://github.com/livekit/rust-sdks) (Apache License 2.0), which is built with [WebRTC](https://webrtc.googlesource.com/src/) and its bundled third-party components. vBot uses WebRTC's audio processing (the AEC3 acoustic echo canceller and high-pass filter). The installed package ships the license texts of WebRTC and every bundled component in `livekit/rtc/resources/LICENSE.md`. WebRTC is distributed under the following BSD 3-Clause License:

Copyright (c) 2011, The WebRTC project authors. All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

- Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
- Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
- Neither the name of Google nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

On Windows the speaker reference is captured with [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) (`pyaudiowpatch`), copyright 2022 S0D3S, distributed under the Apache License 2.0. It is a fork of [PyAudio](https://people.csail.mit.edu/hubert/pyaudio/) 0.2.12, and its extension module includes [PortAudio](https://github.com/PortAudio/portaudio) v19 (revision `8b6d16f26ad660e68a97743842ac29b939f3c0c1`) extended with WASAPI loopback capture. Its package ships only the Apache License, so the PyAudio and PortAudio notices follow.

PyAudio is distributed under the MIT License:

Copyright (c) 2006 Hubert Pham

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

PortAudio is distributed under the following license:

PortAudio Portable Real-Time Audio Library. Copyright (c) 1999-2006 Ross Bencina and Phil Burk

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

The text above constitutes the entire PortAudio license; however, the PortAudio community also makes the following non-binding requests: Any person wishing to distribute modifications to the Software is requested to send the modifications to the original developer so that they can be incorporated into the canonical version. It is also requested that these non-binding requests be included along with the license above.

These libraries remain separately installed dependencies; their source distributions are available from the linked upstream projects.

## Interactive terminal dependencies

The `terminal` Tool uses [pyte](https://github.com/selectel/pyte), distributed under the GNU Lesser General Public License v3, as an in-memory VT terminal emulator. It uses [pywinpty](https://github.com/andfoy/pywinpty), distributed under the MIT License, for ConPTY access on Windows and [ptyprocess](https://github.com/pexpect/ptyprocess), distributed under the ISC License, for PTY process control on POSIX systems. The WebUI Terminals surface uses [xterm.js](https://github.com/xtermjs/xterm.js), including `@xterm/xterm` and `@xterm/addon-fit`, distributed under the MIT License, for browser-side VT rendering and responsive fitting. These libraries remain separately installed dependencies; their source distributions and license texts are available from the linked upstream projects and installed package metadata.


## Web page extraction

The `web_fetch` Tool uses [markdownify](https://github.com/matthewwithanm/python-markdownify), distributed under the MIT License, to preserve HTML structure in readable Markdown. It remains a separately installed dependency; its source distribution and license text are available from the upstream project and installed package metadata.

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
