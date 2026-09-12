const { mount, flushSync, unmount } = await import('svelte');
// @vitest-environment jsdom
import { afterEach, beforeEach, vi } from 'vitest';

import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  transcribeSpeech: vi.fn(),
  uploadAttachment: vi.fn(),
}));

vi.mock('$lib/audioRecorder.js', () => ({
  createAudioRecorder: vi.fn(),
}));

const { transcribeSpeech, uploadAttachment } = await import('$lib/api.js');

const { createAudioRecorder } = await import('$lib/audioRecorder.js');

const { default: ChatComposer } = await import('../ChatComposer.svelte');

const { getDraft, getHistory, pushHistory, resetComposerMemory, setDraft } =
  await import('../../lib/composerMemory.js');

function typeInComposer(input, value, caret = value.length) {
  input.value = value;
  input.setSelectionRange(caret, caret);
  input.dispatchEvent(new InputEvent('input', { bubbles: true }));
  flushSync();
}

function pressKey(input, key) {
  const event = new KeyboardEvent('keydown', {
    key,
    bubbles: true,
    cancelable: true,
  });
  input.dispatchEvent(event);
  flushSync();
  return event;
}

function skillFixtures() {
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

function composerInput() {
  return document.body.querySelector('.msg-input');
}

function cancelRunButton() {
  return Array.from(document.body.querySelectorAll('button')).find(
    (button) => button.getAttribute('aria-label') === 'Cancel run',
  );
}

function autocompleteOptions() {
  return Array.from(
    document.body.querySelectorAll('.skill-autocomplete__option'),
  );
}

function autocompleteNames() {
  return Array.from(
    document.body.querySelectorAll('.skill-autocomplete__name'),
  ).map((element) => element.textContent.trim());
}

function filePickerInput() {
  return document.body.querySelector('.attachment-file-input');
}

async function selectFileFromPicker(file) {
  await selectFilesFromPicker([file]);
}

async function selectFilesFromPicker(files) {
  const input = filePickerInput();
  Object.defineProperty(input, 'files', {
    configurable: true,
    value: files,
  });
  input.dispatchEvent(new Event('change', { bubbles: true }));
  await flushComposerAsyncWork();
}

function submitComposer() {
  document.body
    .querySelector('form.input-area')
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  flushSync();
}

async function flushComposerAsyncWork() {
  await Promise.resolve();
  await Promise.resolve();
  flushSync();
}

function modelCatalogFixture() {
  return {
    models: [
      {
        id: 'openai/gpt-5.2',
        provider_id: 'openai',
        name: 'openai/gpt-5.2',
        capabilities: { tools: true },
        context_window: 128000,
        effective_context_window: 128000,
      },
      {
        id: 'anthropic/claude-sonnet-4',
        provider_id: 'anthropic',
        name: 'anthropic/claude-sonnet-4',
        capabilities: { tools: true },
        context_window: 200000,
        effective_context_window: 200000,
      },
      {
        id: 'ollama/tiny',
        provider_id: 'ollama',
        name: 'ollama/tiny',
        capabilities: { tools: false },
        context_window: 8192,
        effective_context_window: 8192,
      },
    ],
    connections: [
      {
        id: 'openai:api-key',
        provider_id: 'openai',
        label: 'API Key',
        usable: true,
      },
      {
        id: 'anthropic:api-key',
        provider_id: 'anthropic',
        label: 'API Key',
        usable: true,
      },
      {
        id: 'ollama:local',
        provider_id: 'ollama',
        label: 'Local',
        usable: true,
      },
    ],
  };
}

function modelAutocompleteOptions() {
  return Array.from(
    document.body.querySelectorAll('.model-autocomplete__option'),
  );
}

function setupChatComposerSuite() {
  let mountedComponent;
  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    transcribeSpeech.mockReset();
    uploadAttachment.mockReset();
    createAudioRecorder.mockReset();
    localStorage.clear();
    resetComposerMemory();
  });
  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
  });
  return {
    get mountedComponent() {
      return mountedComponent;
    },
    set mountedComponent(value) {
      mountedComponent = value;
    },
  };
}

export {
  transcribeSpeech,
  uploadAttachment,
  createAudioRecorder,
  ChatComposer,
  getDraft,
  getHistory,
  pushHistory,
  setDraft,
  typeInComposer,
  pressKey,
  skillFixtures,
  composerInput,
  cancelRunButton,
  autocompleteOptions,
  autocompleteNames,
  selectFileFromPicker,
  submitComposer,
  flushComposerAsyncWork,
  modelCatalogFixture,
  modelAutocompleteOptions,
  setupChatComposerSuite,
};

export { flushSync, mount, unmount };
