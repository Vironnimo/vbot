// @vitest-environment jsdom
import { afterEach, beforeEach, vi } from 'vitest';
import { flushSync as svelteFlushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  prepareSpeechTranscription: vi.fn(),
  transcribeSpeech: vi.fn(),
  uploadAttachment: vi.fn(),
}));

vi.mock('$lib/audioRecorder.js', () => ({
  createAudioRecorder: vi.fn(),
}));

export const {
  prepareSpeechTranscription,
  transcribeSpeech,
  uploadAttachment,
} = await import('$lib/api.js');
export const { createAudioRecorder } = await import('$lib/audioRecorder.js');
export const { getDraft, getHistory, pushHistory, setDraft } =
  await import('../../lib/composerMemory.js');
const { resetComposerMemory } = await import('../../lib/composerMemory.js');
const { default: ChatComposer } = await import('../ChatComposer.svelte');

export function setupChatComposerSuite() {
  let mountedComponent = null;

  async function unmountComposer() {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
  }

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    prepareSpeechTranscription.mockReset();
    prepareSpeechTranscription.mockResolvedValue({ state: 'loading' });
    transcribeSpeech.mockReset();
    uploadAttachment.mockReset();
    createAudioRecorder.mockReset();
    localStorage.clear();
    resetComposerMemory();
  });

  afterEach(async () => {
    await unmountComposer();
    document.body.innerHTML = '';
  });

  return {
    // Mounts ChatComposer into the body with `props` (a plain object or a
    // reactive props bag).
    mount(props = {}) {
      mountedComponent = mount(ChatComposer, { target: document.body, props });
      flushSync();
      return mountedComponent;
    },
    unmount: unmountComposer,
  };
}

export function flushSync() {
  return svelteFlushSync();
}

// Lets the composer's pending promise chains (sends, pickers, uploads) land.
export async function settle(times = 1) {
  for (let index = 0; index < times; index += 1) {
    await Promise.resolve();
    await Promise.resolve();
    flushSync();
  }
}

export function composerInput() {
  return document.body.querySelector('.msg-input');
}

// Types `value` into the composer with the caret at `caret`.
export function typeInComposer(value, caret = value.length) {
  const input = composerInput();
  input.value = value;
  input.setSelectionRange(caret, caret);
  input.dispatchEvent(new InputEvent('input', { bubbles: true }));
  flushSync();
}

// Presses `key` in the composer (keydown, then keyup unless `keyup` is false)
// and returns the keydown event.
export function pressKey(key, { keyup = true, ...init } = {}) {
  const input = composerInput();
  const event = new KeyboardEvent('keydown', {
    key,
    bubbles: true,
    cancelable: true,
    ...init,
  });
  input.dispatchEvent(event);
  flushSync();
  if (keyup) {
    input.dispatchEvent(new KeyboardEvent('keyup', { key, bubbles: true }));
    flushSync();
  }
  return event;
}

export function submitComposer() {
  document.body
    .querySelector('form.input-area')
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  flushSync();
}

export function buttonLabelled(key) {
  return (
    Array.from(document.body.querySelectorAll('button')).find(
      (button) => button.getAttribute('aria-label') === t(key),
    ) ?? null
  );
}

// Options of the open suggestion popup: `skill` (commands and skills),
// `model`, or `file`.
export function suggestionOptions(kind = 'skill') {
  return Array.from(
    document.body.querySelectorAll(`.${kind}-autocomplete__option`),
  );
}

export function suggestionNames() {
  return Array.from(
    document.body.querySelectorAll('.skill-autocomplete__name'),
  ).map((element) => element.textContent.trim());
}

export async function chooseSuggestion(kind = 'skill', index = 0) {
  suggestionOptions(kind)[index].dispatchEvent(
    new MouseEvent('click', { bubbles: true }),
  );
  await settle();
}

export async function selectFilesFromPicker(...files) {
  const input = document.body.querySelector('.attachment-file-input');
  Object.defineProperty(input, 'files', { configurable: true, value: files });
  input.dispatchEvent(new Event('change', { bubbles: true }));
  await settle();
}

// An uploaded attachment as `uploadAttachment` returns it.
export function uploaded(attachmentId, filename, mediaType, sizeBytes = 11) {
  return {
    attachment_id: attachmentId,
    filename,
    media_type: mediaType,
    size_bytes: sizeBytes,
  };
}

export function skillFixtures() {
  return [
    {
      name: 'debugging',
      description: 'Investigate unclear bugs.',
      valid: true,
    },
    {
      name: 'frontend-design',
      description: 'Create polished interfaces.',
      valid: true,
    },
  ];
}

// Three models: two tool-capable ones that `/model` offers and one without
// tools that it hides.
export function modelCatalogFixture() {
  const model = (provider, id, tools, contextWindow) => ({
    id: `${provider}/${id}`,
    provider_id: provider,
    name: `${provider}/${id}`,
    capabilities: { tools },
    context_window: contextWindow,
    effective_context_window: contextWindow,
  });
  return {
    models: [
      model('openai', 'gpt-5.2', true, 128000),
      model('anthropic', 'claude-sonnet-4', true, 200000),
      model('ollama', 'tiny', false, 8192),
    ],
    connections: [
      { id: 'openai:api-key', provider_id: 'openai', label: 'API Key' },
      { id: 'anthropic:api-key', provider_id: 'anthropic', label: 'API Key' },
      { id: 'ollama:local', provider_id: 'ollama', label: 'Local' },
    ].map((connection) => ({ ...connection, usable: true })),
  };
}

// A promise with its resolve function, for holding a callback's result.
export function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}
