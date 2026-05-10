// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: ChatComposer } = await import('../ChatComposer.svelte');

describe('ChatComposer', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
  });

  it('offers slash skill autocomplete at the start of the message', async () => {
    mountedComponent = mount(ChatComposer, {
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

  it('inserts inline skill triggers without rewriting the message', async () => {
    const onSendMessage = vi.fn();
    mountedComponent = mount(ChatComposer, {
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
    mountedComponent = mount(ChatComposer, {
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
});

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
  return document.body.querySelector('#chat-composer-input');
}
