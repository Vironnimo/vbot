# Third-Party Notices

## Hey Nabu v2 wake-word model

vBot includes `desktop/wakeword/models/hey_nabu_v2.tflite` from the [Home Assistant Wake Words Collection](https://github.com/fwartner/home-assistant-wakewords-collection/tree/main/en/hey_nabu), copyright 2023 Florian Wartner. The bundled file has SHA-256 `ce18b69e1bddfb56e70fe739d6ca0f423f70a6e710f05b376baf6a3625689234` and is distributed under the MIT License:

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## pyopen-wakeword audio test fixtures

`tests/fixtures/wakeword/okay_nabu.wav` and `tests/fixtures/wakeword/unrelated_hey_jarvis.wav` come from the corresponding test directories in [rhasspy/pyopen-wakeword](https://github.com/rhasspy/pyopen-wakeword/tree/main/tests) and are distributed under that project's Apache License 2.0. Their SHA-256 values are `c19747e603b00db74eb53ee2a65ae0489dd9feb574a9e98683fc3be8740b6c66` and `05bf58195bd9c6af46becd373565c87bf0e57d133a950f2c0aa441daf1acb908`, respectively.

`tests/fixtures/wakeword/hey_nabu.wav` is a generated test fixture containing the spoken phrase "Hey Naboo", synthesized with the Microsoft Zira Desktop voice at rate -2. Its SHA-256 is `2bc6ddba7c57e6451de96d621bba95479b24085677f58fc933861e102e966679`.
