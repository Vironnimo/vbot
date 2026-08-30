# Third-Party Notices

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
