// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const api = vi.hoisted(() => ({
  answerSecretRequest: vi.fn(),
  declineSecretRequest: vi.fn(),
  listAgentSecrets: vi.fn(),
}));
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () => api);
const { default: SecretRequests } =
  await import('../chat/SecretRequests.svelte');

const request = (id, name, overrides = {}) => ({
  id,
  name,
  agent_id: 'main',
  session_id: 'sess-1',
  kind: 'login',
  description: 'For the deploy',
  lifetime: 'permanent',
  replaces: false,
  fields: [
    { name: 'username', concealed: false, optional: false },
    { name: 'password', concealed: true, optional: false },
  ],
  expires_at: '2026-10-21T12:00:00Z',
  ...overrides,
});
const listed = (requests) => ({ secrets: [], sources: [], requests });

let component;
let props;

beforeEach(() => {
  document.body.innerHTML = '';
  init('en');
  vi.useFakeTimers();
  for (const fn of Object.values(api)) fn.mockReset();
});

afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  vi.useRealTimers();
  document.body.innerHTML = '';
});

async function settle() {
  await vi.advanceTimersByTimeAsync(0);
  flushSync();
}

async function render() {
  props = reactiveProps({
    agentId: 'main',
    sessionId: 'sess-1',
    refreshToken: 0,
  });
  component = mount(SecretRequests, { target: document.body, props });
  await settle();
}

async function click(label) {
  const scope = document.querySelector('[role="dialog"]') ?? document;
  const match = [...scope.querySelectorAll('button')].find(
    (item) => item.textContent.trim() === label,
  );
  expect(match, `button ${label}`).toBeTruthy();
  match.click();
  await settle();
}

function field(label) {
  const labelElement = [
    ...document.querySelectorAll('[role="dialog"] label'),
  ].find((item) => item.textContent.replace('*', '').trim() === label);
  expect(labelElement, `field ${label}`).toBeTruthy();
  return document.getElementById(labelElement.htmlFor);
}

async function type(label, value) {
  const input = field(label);
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  await settle();
}

async function store() {
  document
    .querySelector('[role="dialog"] form')
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  await settle();
}

const notices = () =>
  [...document.querySelectorAll('[data-secret-request]')].map(
    (item) => item.dataset.secretRequest,
  );

describe('SecretRequests', () => {
  it('stores the requested values with the requested lifetime for the open Session only', async () => {
    api.listAgentSecrets.mockResolvedValue(
      listed([
        request('req-1', 'GITHUB'),
        request('req-2', 'OTHER', { session_id: 'sess-2' }),
        request('req-3', 'PROJECT', { project_id: 'alpha' }),
        request('req-4', 'AGENT', { agent_id: 'helper' }),
      ]),
    );
    api.answerSecretRequest.mockResolvedValue({});
    await render();
    expect(notices()).toEqual(['GITHUB']);

    await click('Enter value');
    expect(field('Password').type).toBe('password');
    await store();
    expect(api.answerSecretRequest).not.toHaveBeenCalled();
    expect(document.querySelector('[role="dialog"]').textContent).toContain(
      'Enter a value.',
    );

    await type('Username', 'octo');
    await type('Password', 'pw-value-123');
    await store();
    expect(api.answerSecretRequest).toHaveBeenCalledWith({
      id: 'req-1',
      values: { username: 'octo', password: 'pw-value-123' },
      lifetime: 'permanent',
    });
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(notices()).toEqual([]);
    props.projectId = 'alpha';
    await settle();
    expect(notices()).toEqual(['PROJECT']);
  });

  it('declines a request and drops a notice once the request is gone', async () => {
    api.listAgentSecrets.mockResolvedValue(
      listed([request('req-1', 'GITHUB'), request('req-3', 'NPM_TOKEN')]),
    );
    api.declineSecretRequest.mockResolvedValue({});
    await render();

    const decline = [
      ...document.querySelectorAll('[data-secret-request="GITHUB"] button'),
    ].find((item) => item.textContent.trim() === 'Decline');
    decline.click();
    await settle();
    expect(api.declineSecretRequest).toHaveBeenCalledWith('req-1');
    expect(notices()).toEqual(['NPM_TOKEN']);

    await click('Enter value');
    api.listAgentSecrets.mockResolvedValue(listed([]));
    props.refreshToken = 1;
    await settle();
    expect(notices()).toEqual([]);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });
});
