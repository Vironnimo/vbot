// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  rpc: (...args) => rpcMock(...args),
}));

const { default: AgentsView } = await import('../AgentsView.svelte');

describe('AgentsView', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
  });

  it('renders model dropdown options using canonical model ids', async () => {
    rpcMock.mockImplementation(async (method) => {
      if (method === 'model.list') {
        return {
          models: [
            {
              id: 'anthropic/claude-sonnet-4-20250219',
              provider_id: 'anthropic',
              model_id: 'claude-sonnet-4-20250219',
              name: 'Claude Sonnet 4',
            },
            {
              id: 'openai/gpt-5.2',
              provider_id: 'openai',
              model_id: 'gpt-5.2',
              name: 'GPT-5.2',
            },
          ],
        };
      }

      if (method === 'tool.list') {
        return { tools: [] };
      }

      if (method === 'agent.list') {
        return {
          agents: [
            {
              id: 'alpha',
              name: 'Alpha',
              model: 'openai/gpt-5.2',
              fallback_model: 'anthropic/claude-sonnet-4-20250219',
              workspace: 'C:/agents/alpha',
              current_session_id: 'session-1',
              temperature: '',
              thinking_effort: '',
              allowed_tools: ['*'],
              allowed_skills: ['*'],
              created_at: '2026-05-08T00:00:00+00:00',
              updated_at: '2026-05-08T00:00:00+00:00',
            },
          ],
        };
      }

      throw new Error(`Unexpected RPC method: ${method}`);
    });

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(() => {
      const [modelSelect, fallbackModelSelect] = getSelects();

      return (
        Array.from(modelSelect?.options ?? []).some(
          (option) => option.textContent === 'openai/gpt-5.2',
        ) &&
        Array.from(fallbackModelSelect?.options ?? []).some(
          (option) =>
            option.textContent === 'anthropic/claude-sonnet-4-20250219',
        )
      );
    }, 100);

    const [modelSelect, fallbackModelSelect] = getSelects();
    const modelOptionLabels = Array.from(modelSelect.options).map(
      (option) => option.textContent,
    );
    const fallbackOptionLabels = Array.from(fallbackModelSelect.options).map(
      (option) => option.textContent,
    );

    expect(modelOptionLabels).toContain('openai/gpt-5.2');
    expect(modelOptionLabels).toContain('anthropic/claude-sonnet-4-20250219');
    expect(modelOptionLabels).not.toContain('openai / GPT-5.2');
    expect(modelOptionLabels).not.toContain('anthropic / Claude Sonnet 4');
    expect(fallbackOptionLabels).toContain('openai/gpt-5.2');
    expect(fallbackOptionLabels).toContain(
      'anthropic/claude-sonnet-4-20250219',
    );
  });

  it('preserves a saved unavailable model value in the dropdown', async () => {
    rpcMock.mockImplementation(async (method) => {
      if (method === 'model.list') {
        return {
          models: [
            {
              id: 'openai/gpt-5.2',
              provider_id: 'openai',
              model_id: 'gpt-5.2',
              name: 'GPT-5.2',
            },
          ],
        };
      }

      if (method === 'tool.list') {
        return { tools: [] };
      }

      if (method === 'agent.list') {
        return {
          agents: [
            {
              id: 'alpha',
              name: 'Alpha',
              model: 'legacy/custom-model',
              fallback_model: '',
              workspace: 'C:/agents/alpha',
              current_session_id: 'session-1',
              temperature: '',
              thinking_effort: '',
              allowed_tools: ['*'],
              allowed_skills: ['*'],
              created_at: '2026-05-08T00:00:00+00:00',
              updated_at: '2026-05-08T00:00:00+00:00',
            },
          ],
        };
      }

      throw new Error(`Unexpected RPC method: ${method}`);
    });

    mountedComponent = mount(AgentsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () =>
        getSelects()[0]?.value === 'legacy/custom-model' &&
        Array.from(getSelects()[0]?.options ?? []).some(
          (option) => option.textContent === 'openai/gpt-5.2',
        ),
      100,
    );

    const [modelSelect] = getSelects();
    const modelOptionLabels = Array.from(modelSelect.options).map(
      (option) => option.textContent,
    );

    expect(modelSelect.value).toBe('legacy/custom-model');
    expect(modelOptionLabels).toContain(
      'Unavailable / custom: legacy/custom-model',
    );
    expect(modelOptionLabels).toContain('openai/gpt-5.2');
  });
});

function getSelects() {
  return Array.from(document.body.querySelectorAll('select'));
}

async function waitForCondition(check, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();

    if (check()) {
      return;
    }
  }

  throw new Error('Timed out waiting for condition.');
}
