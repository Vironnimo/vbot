import { expect, it, vi } from 'vitest';
import {
  readSpeechPlayback,
  RPC_ERROR_RESPONSE,
  RPC_ERROR_INVALID_CLIENT_REQUEST,
} from '../api.js';

function frame(rate, payload = []) {
  const bytes = new Uint8Array(8 + payload.length);
  const view = new DataView(bytes.buffer);
  view.setUint32(0, rate, true);
  view.setUint32(4, payload.length, true);
  bytes.set(payload, 8);
  return bytes;
}
function transport(parts) {
  return vi.fn(
    async () =>
      new Response(
        new ReadableStream({
          start(controller) {
            for (const part of parts) controller.enqueue(part);
            controller.close();
          },
        }),
      ),
  );
}
async function collect(iterator) {
  const frames = [];
  for await (const frame of iterator) frames.push(frame);
  return frames;
}

it('decodes fragmented PCM before the success terminal and supports rate changes', async () => {
  const first = frame(16000, [0, 0, 255, 127]);
  const fetch = transport([
    first.slice(0, 3),
    first.slice(3, 9),
    first.slice(9),
    frame(24000, [0, 128]),
    frame(0),
  ]);
  const received = await collect(
    readSpeechPlayback('/api/speech/playback/test', { fetch }),
  );
  expect(received).toEqual([
    { sampleRate: 16000, bytes: new Uint8Array([0, 0, 255, 127]) },
    { sampleRate: 24000, bytes: new Uint8Array([0, 128]) },
  ]);
});

it.each([
  ['unsealed EOF', [frame(16000, [0, 0])]],
  ['failure terminal', [frame(0, new TextEncoder().encode('failed'))]],
  ['odd PCM bytes', [frame(16000, [0])]],
  ['oversized frame', [frame(16000, new Uint8Array(32770))]],
  ['truncated header', [new Uint8Array(3)]],
])('rejects %s rather than accepting partial playback', async (_, parts) => {
  await expect(
    collect(
      readSpeechPlayback('/api/speech/playback/test', {
        fetch: transport(parts),
      }),
    ),
  ).rejects.toMatchObject({ code: RPC_ERROR_RESPONSE });
});

it('refuses an external playback URL before requesting it', async () => {
  const fetch = vi.fn();
  await expect(
    collect(readSpeechPlayback('https://external.example/voice', { fetch })),
  ).rejects.toMatchObject({ code: RPC_ERROR_INVALID_CLIENT_REQUEST });
  expect(fetch).not.toHaveBeenCalled();
});
