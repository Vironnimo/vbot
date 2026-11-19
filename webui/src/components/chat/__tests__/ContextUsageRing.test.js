// @vitest-environment jsdom

// The ring's card, levels and Compaction action are covered through the Chat
// composer (ChatComposer.test.js -> context usage card); this suite covers
// the readout's own variants.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { reactiveProps } from '../../__tests__/reactiveProps.support.svelte.js';
import { init, t } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: ContextUsageRing } =
  await import('../ContextUsageRing.svelte');

describe('ContextUsageRing', () => {
  let component;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
  });

  afterEach(() => {
    if (component) {
      unmount(component);
      component = null;
    }
    vi.useRealTimers();
    document.body.innerHTML = '';
  });

  function render(props) {
    component = mount(ContextUsageRing, { target: document.body, props });
    flushSync();
    return {
      trigger: document.querySelector('.context-ring-trigger'),
      card: document.querySelector('.context-card'),
    };
  }

  it('shows compact figures in its name and exact counts in the card', () => {
    const { trigger, card } = render({
      contextUsage: { tokens: 51234, estimated: false },
      contextWindow: 1_000_000,
      usage: { input_tokens: 51000, output_tokens: 234 },
      showFigures: true,
    });

    const figures = trigger.querySelector('.context-ring__figures').textContent;
    expect(figures).toBe('51.2K / 1M · 5%');
    expect(trigger.getAttribute('aria-label')).toBe(
      t('chat.contextRingFiguresLabel', { figures }),
    );
    expect(card.querySelector('.context-card__usage').textContent).toBe(
      '51,234 / 1,000,000',
    );
    expect(card.querySelector('.context-card__action')).toBeNull();
  });

  it('shows remaining windows and balance only for the matching account, and expires them on reset', () => {
    vi.useFakeTimers();
    const now = Date.parse('2026-11-19T12:00:00Z');
    vi.setSystemTime(now);
    const route = {
      name: 'OpenRouter',
      connection: 'openrouter:api-key',
      account: 'work',
    };
    const snapshot = {
      connection: route.connection,
      account: route.account,
      display_name: route.name,
      observed_at: new Date(now - 8 * 60_000).toISOString(),
      windows: [
        {
          label: '5h',
          used_percent: 70,
          reset_at: new Date(now + 1000).toISOString(),
        },
      ],
      credits: { enabled: true, balance: 0, unit: 'USD' },
    };
    const props = reactiveProps({
      providerLimits: { name: route.name, observation: snapshot },
      contextUsage: { tokens: 12 },
      contextWindow: 100,
    });
    const { trigger, card } = render(props);
    trigger.parentElement.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(500);
    flushSync();
    const section = card.querySelector('.provider-usage');
    expect(section.querySelector('h3').textContent).toMatch(
      /OpenRouter.*\(8 min\. ago\)/,
    );
    expect(section.textContent).toContain('30%');
    expect(section.textContent).toContain('$0.00');
    vi.advanceTimersByTime(1000);
    flushSync();
    expect(section.textContent).not.toContain('30%');
    props.providerLimits = { name: route.name };
    flushSync();
    expect(section.textContent.trim()).toBe('OpenRouter');
  });

  it('renders nothing until the tokens and the window are known', () => {
    render({
      contextUsage: { tokens: 120, estimated: false },
      contextWindow: null,
      showFigures: true,
    });

    expect(document.querySelector('.context-ring')).toBeNull();
  });
});
