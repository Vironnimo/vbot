// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: FileAutocomplete } =
  await import('../FileAutocomplete.svelte');
const { default: SkillAutocomplete } =
  await import('../SkillAutocomplete.svelte');
const { default: ModelAutocomplete } =
  await import('../ModelAutocomplete.svelte');

describe.each([
  [
    FileAutocomplete,
    { candidates: [{ path: 'notes.md', kind: 'file', ignored: false }] },
  ],
  [SkillAutocomplete, { skills: [{ name: 'debugging' }] }],
  [ModelAutocomplete, { options: [{ value: 'openai/gpt-5.2' }] }],
])('autocomplete explicit active selection', (Component, componentProps) => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
    }
    document.body.innerHTML = '';
  });

  it('does not select the first item when the active index is out of range', () => {
    const onSelect = vi.fn();
    mountedComponent = mount(Component, {
      target: document.body,
      props: {
        ...componentProps,
        activeIndex: 4,
        onSelect,
      },
    });
    flushSync();

    expect(document.querySelector('[role="option"].active')).toBeNull();
    expect(mountedComponent.selectActive()).toBe(false);
    expect(onSelect).not.toHaveBeenCalled();
  });
});

describe('skill autocomplete', () => {
  let mountedComponent;

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
    }
    document.body.innerHTML = '';
  });

  // Commands and Skills share one list, so a Skill may carry a command's name.
  it('lists a command and a Skill of the same name as separate rows', () => {
    init('en');
    const command = { name: 'handoff', type: 'command', argument: 'optional' };
    const skill = { name: 'handoff', type: 'skill' };
    const onSelect = vi.fn();
    mountedComponent = mount(SkillAutocomplete, {
      target: document.body,
      props: {
        skills: [command, skill],
        query: 'hand',
        activeIndex: 1,
        onSelect,
      },
    });
    flushSync();

    expect(document.querySelectorAll('[role="option"]')).toHaveLength(2);
    expect(mountedComponent.selectActive()).toBe(true);
    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'handoff', type: 'skill' }),
    );
  });
});

describe('model autocomplete', () => {
  let mountedComponent;

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
    }
    document.body.innerHTML = '';
  });

  it('keeps the option marker of a Model option', () => {
    init('en');
    mountedComponent = mount(ModelAutocomplete, {
      target: document.body,
      props: {
        options: [
          { value: 'demo/verified', marker: { label: 'Verified' } },
          { value: 'demo/inferred' },
        ],
      },
    });
    flushSync();

    const [verified, inferred] = document.querySelectorAll('[role="option"]');
    expect(
      verified.querySelector('[role="img"][aria-label="Verified"]'),
    ).toBeTruthy();
    expect(inferred.querySelector('.option-marker')).toBeNull();
  });
});
