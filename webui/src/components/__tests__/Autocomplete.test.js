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
  [FileAutocomplete, { files: ['notes.md'] }],
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
