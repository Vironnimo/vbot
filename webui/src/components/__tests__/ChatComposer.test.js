// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  mount,
  ChatComposer,
  typeInComposer,
  skillFixtures,
  composerInput,
  autocompleteOptions,
  autocompleteNames,
  submitComposer,
  flushComposerAsyncWork,
  modelCatalogFixture,
  modelAutocompleteOptions,
  setupChatComposerSuite,
} from './ChatComposer.support.js';

describe('ChatComposer', () => {
  const suite = setupChatComposerSuite();

  it('keeps the context card open across pointer travel and invokes compaction', async () => {
    vi.useFakeTimers();
    try {
      const onForceCompaction = vi.fn();
      suite.mountedComponent = mount(ChatComposer, {
        target: document.body,
        props: {
          contextUsage: { tokens: 4000, estimated: true },
          contextWindow: 10000,
          compactionState: 'idle',
          onForceCompaction,
        },
      });
      flushSync();
      const anchor = document.querySelector('.context-ring');
      anchor.dispatchEvent(new Event('pointerenter'));
      const card = document.querySelector('.context-hover-card');
      expect(card.parentElement).toBe(document.body);
      expect(card.dataset.floatingOpen).toBe('true');
      anchor.dispatchEvent(new Event('pointerleave'));
      card.dispatchEvent(new Event('pointerenter'));
      await vi.advanceTimersByTimeAsync(500);
      expect(card.dataset.floatingOpen).toBe('true');
      card.querySelector('button').click();
      expect(onForceCompaction).toHaveBeenCalledTimes(1);
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
      expect(card.dataset.floatingOpen).not.toBe('true');
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(['pending', 'running', 'unavailable'])(
    'disables compaction while %s',
    (compactionState) => {
      const onForceCompaction = vi.fn();
      suite.mountedComponent = mount(ChatComposer, {
        target: document.body,
        props: {
          contextUsage: { tokens: 4000 },
          contextWindow: 10000,
          compactionState,
          onForceCompaction,
        },
      });
      flushSync();
      const button = document.querySelector('.context-hover-card button');
      expect(button.disabled).toBe(true);
      button.click();
      expect(onForceCompaction).not.toHaveBeenCalled();
    },
  );

  it('offers slash skill autocomplete at the start of the message', async () => {
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { availableSkills: skillFixtures() },
    });
    flushSync();

    const input = composerInput();
    input.value = '/deb';
    input.setSelectionRange(4, 4);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    expect(document.body.textContent).toContain('debugging');
    expect(document.body.textContent).toContain('Investigate unclear bugs.');

    document.body
      .querySelector('.skill-autocomplete__option')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
    flushSync();

    expect(input.value).toBe('/debugging');
  });

  it('normalizes slash command names when inserting from autocomplete', async () => {
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: {
        availableSkills: [
          {
            name: '/compact',
            description: 'Compact the current session context.',
            type: 'command',
          },
        ],
      },
    });
    flushSync();

    const input = composerInput();
    input.value = '/com';
    input.setSelectionRange(4, 4);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    document.body
      .querySelector('.skill-autocomplete__option')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
    flushSync();

    expect(input.value).toBe('/compact');
  });

  it('runs a no-argument command immediately without inserting it', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: {
        onSendMessage,
        availableSkills: [
          {
            name: 'status',
            description: 'Show current session and runtime status.',
            type: 'command',
            argument: 'none',
          },
        ],
      },
    });
    flushSync();

    const input = composerInput();
    input.value = '/stat';
    input.setSelectionRange(5, 5);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    document.body
      .querySelector('.skill-autocomplete__option')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await flushComposerAsyncWork();

    expect(onSendMessage).toHaveBeenCalledWith('/status');
    expect(input.value).toBe('');
  });

  it('does not run an immediate command while another submit is in flight', async () => {
    let resolveSend;
    const onSendMessage = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveSend = resolve;
        }),
    );
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: {
        onSendMessage,
        availableSkills: [
          {
            name: 'status',
            description: 'Show current session and runtime status.',
            type: 'command',
            argument: 'none',
          },
        ],
      },
    });
    flushSync();

    typeInComposer(composerInput(), 'first');
    submitComposer();
    expect(onSendMessage).toHaveBeenCalledTimes(1);

    typeInComposer(composerInput(), '/stat');
    document.body
      .querySelector('.skill-autocomplete__option')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
    flushSync();

    expect(onSendMessage).toHaveBeenCalledTimes(1);
    expect(composerInput().value).toBe('/stat');

    resolveSend(true);
    await flushComposerAsyncWork();
  });

  it('inserts an argument-bearing command instead of running it', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: {
        onSendMessage,
        availableSkills: [
          {
            name: 'compact',
            description: 'Compact the current session context.',
            type: 'command',
            argument: 'optional',
          },
        ],
      },
    });
    flushSync();

    const input = composerInput();
    input.value = '/com';
    input.setSelectionRange(4, 4);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    document.body
      .querySelector('.skill-autocomplete__option')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
    flushSync();

    expect(input.value).toBe('/compact');
    expect(onSendMessage).not.toHaveBeenCalled();
  });

  it('offers only skills for inline dollar autocomplete', async () => {
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: {
        availableSkills: [
          {
            name: 'stop',
            description: 'Cancel the active run.',
            type: 'command',
          },
          {
            name: 'debugging',
            description: 'Investigate unclear bugs.',
            type: 'skill',
          },
        ],
      },
    });
    flushSync();

    const input = composerInput();
    input.value = 'Please use $';
    input.setSelectionRange(12, 12);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    expect(autocompleteNames()).toEqual(['debugging']);
    expect(
      document.body.querySelector('.skill-autocomplete__eyebrow').textContent,
    ).toContain('skills');
  });

  it('inserts inline skill triggers without rewriting the message', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { availableSkills: skillFixtures(), onSendMessage },
    });
    flushSync();

    const input = composerInput();
    input.value = 'Please use $deb here.  ';
    input.setSelectionRange(15, 15);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );
    await Promise.resolve();
    flushSync();

    expect(input.value).toBe('Please use $debugging here.  ');

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );
    flushSync();

    expect(onSendMessage).toHaveBeenCalledWith('Please use $debugging here.  ');
  });

  it('includes loadable warning skills in autocomplete', async () => {
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: {
        availableSkills: [
          ...skillFixtures(),
          {
            name: 'warning-skill',
            description: 'Loadable with validation warnings.',
            valid: false,
            warnings: ['Skill name differs from directory name.'],
          },
        ],
      },
    });
    flushSync();

    const input = composerInput();
    input.value = '$warning';
    input.setSelectionRange(8, 8);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    expect(document.body.textContent).toContain('warning-skill');
    expect(document.body.textContent).toContain(
      'Loadable with validation warnings.',
    );

    document.body
      .querySelector('.skill-autocomplete__option')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
    flushSync();

    expect(input.value).toBe('$warning-skill');
  });

  it('keeps slash autocomplete keyboard navigation after arrow keyup', () => {
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: {
        availableSkills: [
          ...skillFixtures(),
          {
            name: 'status',
            description: 'Show runtime status.',
            valid: true,
          },
        ],
      },
    });
    flushSync();

    const input = composerInput();
    input.value = '/';
    input.setSelectionRange(1, 1);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    expect(autocompleteOptions()).toHaveLength(3);
    expect(autocompleteOptions()[0].getAttribute('aria-selected')).toBe('true');

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }),
    );
    flushSync();
    input.dispatchEvent(
      new KeyboardEvent('keyup', { key: 'ArrowDown', bubbles: true }),
    );
    flushSync();

    expect(autocompleteOptions()[1].getAttribute('aria-selected')).toBe('true');

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }),
    );
    flushSync();
    input.dispatchEvent(
      new KeyboardEvent('keyup', { key: 'ArrowDown', bubbles: true }),
    );
    flushSync();

    expect(autocompleteOptions()[2].getAttribute('aria-selected')).toBe('true');

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true }),
    );
    flushSync();
    input.dispatchEvent(
      new KeyboardEvent('keyup', { key: 'ArrowUp', bubbles: true }),
    );
    flushSync();

    expect(autocompleteOptions()[1].getAttribute('aria-selected')).toBe('true');
  });

  it('lets keyboard navigation reach every rendered match (no cap)', () => {
    const manySkills = Array.from({ length: 9 }, (_item, index) => ({
      name: `skill-${index + 1}`,
      description: `Skill number ${index + 1}.`,
      valid: true,
    }));
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { availableSkills: manySkills },
    });
    flushSync();

    const input = composerInput();
    input.value = '/';
    input.setSelectionRange(1, 1);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    // All nine render (the popup is scrollable); arrow-key navigation must be
    // able to reach the last one. A stale count cap stopped the active index at
    // the eighth entry, leaving the ninth unreachable by keyboard.
    expect(autocompleteOptions()).toHaveLength(9);

    for (let step = 0; step < 8; step += 1) {
      input.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }),
      );
      flushSync();
      input.dispatchEvent(
        new KeyboardEvent('keyup', { key: 'ArrowDown', bubbles: true }),
      );
      flushSync();
    }

    expect(autocompleteOptions()[8].getAttribute('aria-selected')).toBe('true');
  });

  it('keeps popup closed after Escape keyup', () => {
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { availableSkills: skillFixtures() },
    });
    flushSync();

    const input = composerInput();
    input.value = '/deb';
    input.setSelectionRange(4, 4);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    expect(autocompleteOptions()).toHaveLength(1);

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    );
    flushSync();
    input.dispatchEvent(
      new KeyboardEvent('keyup', { key: 'Escape', bubbles: true }),
    );
    flushSync();

    expect(autocompleteOptions()).toHaveLength(0);
  });

  it('keeps popup closed after Enter selection keyup (slash)', async () => {
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { availableSkills: skillFixtures() },
    });
    flushSync();

    const input = composerInput();
    input.value = '/deb';
    input.setSelectionRange(4, 4);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    expect(autocompleteOptions()).toHaveLength(1);

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );
    await Promise.resolve();
    flushSync();

    input.dispatchEvent(
      new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }),
    );
    flushSync();

    expect(input.value).toBe('/debugging');
    expect(autocompleteOptions()).toHaveLength(0);
  });

  it('keeps popup closed after Enter selection keyup ($skill inline)', async () => {
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { availableSkills: skillFixtures() },
    });
    flushSync();

    const input = composerInput();
    input.value = 'use $deb here';
    input.setSelectionRange(8, 8);
    input.dispatchEvent(new InputEvent('input', { bubbles: true }));
    flushSync();

    expect(autocompleteOptions()).toHaveLength(1);

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );
    await Promise.resolve();
    flushSync();

    input.dispatchEvent(
      new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }),
    );
    flushSync();

    expect(input.value).toContain('$debugging');
    expect(autocompleteOptions()).toHaveLength(0);
  });

  it('opens the model argument autocomplete after "/model "', async () => {
    const onLoadModelCatalog = vi.fn().mockResolvedValue(modelCatalogFixture());
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onLoadModelCatalog },
    });
    flushSync();

    typeInComposer(composerInput(), '/model ');
    await flushComposerAsyncWork();

    expect(onLoadModelCatalog).toHaveBeenCalledTimes(1);
    const options = modelAutocompleteOptions();
    expect(options.length).toBeGreaterThan(0);
    expect(options[0].textContent).toContain('openai/gpt-5.2');
  });

  it('filters model options by the text after "/model "', async () => {
    const onLoadModelCatalog = vi.fn().mockResolvedValue(modelCatalogFixture());
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onLoadModelCatalog },
    });
    flushSync();

    typeInComposer(composerInput(), '/model ');
    await flushComposerAsyncWork();

    // All suitable models are visible initially.
    expect(modelAutocompleteOptions().length).toBe(2);

    // Typing a fragment narrows the list.
    typeInComposer(composerInput(), '/model ant');
    await flushComposerAsyncWork();

    const filtered = modelAutocompleteOptions();
    expect(filtered).toHaveLength(1);
    expect(filtered[0].textContent).toContain('anthropic/claude-sonnet-4');
  });

  it('submits "/model <value>" immediately when a model is selected', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    const onLoadModelCatalog = vi.fn().mockResolvedValue(modelCatalogFixture());
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onSendMessage, onLoadModelCatalog },
    });
    flushSync();

    typeInComposer(composerInput(), '/model ');
    await flushComposerAsyncWork();

    modelAutocompleteOptions()[0].dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    );
    await flushComposerAsyncWork();

    expect(onSendMessage).toHaveBeenCalledWith(
      '/model openai/gpt-5.2::api-key',
    );
    expect(composerInput().value).toBe('');
  });

  it('does not submit while another send is in flight', async () => {
    let resolveSend;
    const onSendMessage = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveSend = resolve;
        }),
    );
    const onLoadModelCatalog = vi.fn().mockResolvedValue(modelCatalogFixture());
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onSendMessage, onLoadModelCatalog },
    });
    flushSync();

    typeInComposer(composerInput(), 'first message');
    submitComposer();
    expect(onSendMessage).toHaveBeenCalledTimes(1);

    typeInComposer(composerInput(), '/model ');
    await flushComposerAsyncWork();

    modelAutocompleteOptions()[0].dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    );
    await flushComposerAsyncWork();

    expect(onSendMessage).toHaveBeenCalledTimes(1);
    expect(composerInput().value).toBe('/model ');

    resolveSend(true);
    await flushComposerAsyncWork();
  });

  it('does not open the model popup for "/modeling" or other slash text', async () => {
    const onLoadModelCatalog = vi.fn().mockResolvedValue(modelCatalogFixture());
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { availableSkills: skillFixtures(), onLoadModelCatalog },
    });
    flushSync();

    typeInComposer(composerInput(), '/modeling something');
    await flushComposerAsyncWork();

    expect(onLoadModelCatalog).not.toHaveBeenCalled();
    expect(document.body.querySelector('.model-autocomplete')).toBeNull();
  });

  it('lets Enter select the active model option', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(true);
    const onLoadModelCatalog = vi.fn().mockResolvedValue(modelCatalogFixture());
    suite.mountedComponent = mount(ChatComposer, {
      target: document.body,
      props: { onSendMessage, onLoadModelCatalog },
    });
    flushSync();

    typeInComposer(composerInput(), '/model ');
    await flushComposerAsyncWork();

    const input = composerInput();
    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );
    await flushComposerAsyncWork();

    expect(onSendMessage).toHaveBeenCalledWith(
      '/model openai/gpt-5.2::api-key',
    );
  });
});
