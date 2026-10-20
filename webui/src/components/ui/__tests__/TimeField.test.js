// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';
import { reactiveProps } from '../../__tests__/reactiveProps.support.svelte.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: TimeField } = await import('../TimeField.svelte');

const spaced = (text) => text.replace(/\s/g, ' ').trim();

describe('TimeField', () => {
  let mounted;
  let props;
  let changes;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    changes = [];
  });

  afterEach(async () => {
    if (mounted) await unmount(mounted);
    mounted = null;
    document.body.innerHTML = '';
  });

  function render(initial = {}) {
    props = reactiveProps({
      value: '09:15',
      ariaLabel: 'Start time',
      onChange: (next) => {
        changes.push(next);
        props.value = next;
      },
      ...initial,
    });
    mounted = mount(TimeField, { target: document.body, props });
    flushSync();
    return document.body.querySelector('input');
  }

  function type(input, text) {
    input.value = text;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
  }

  function press(target, key) {
    const event = new KeyboardEvent('keydown', {
      key,
      bubbles: true,
      cancelable: true,
    });
    target.dispatchEvent(event);
    flushSync();
    return event;
  }

  async function settle() {
    for (let round = 0; round < 4; round += 1) await Promise.resolve();
    flushSync();
  }

  const list = () => document.body.querySelector('.time-field__list');
  const activeOption = (input) =>
    document.getElementById(input.getAttribute('aria-activedescendant'));

  it('shows its time on the app language clock', () => {
    const input = render({ value: '21:30' });

    expect(spaced(input.value)).toBe('9:30 PM');
  });

  it.each([
    ['9', '09:00'],
    ['930', '09:30'],
    ['9:30', '09:30'],
    ['9.30', '09:30'],
    ['2130', '21:30'],
    ['9:30 pm', '21:30'],
    ['9p', '21:00'],
    ['12am', '00:00'],
    ['12 PM', '12:00'],
  ])('reads the typed time %j as %s', (text, expected) => {
    const input = render();

    type(input, text);

    expect(changes).toEqual([expected]);
  });

  it('keeps its time through text that is no time and shows it again on leaving', () => {
    const input = render();
    input.focus();

    for (const text of ['25', '9:75', '13pm', 'noon']) type(input, text);
    expect(changes).toEqual([]);

    input.blur();
    flushSync();
    expect(spaced(input.value)).toBe('9:15 AM');
  });

  it('clears only when the time is optional', () => {
    const input = render({ required: true });
    type(input, '');
    expect(changes).toEqual([]);

    unmount(mounted);
    mounted = null;
    document.body.innerHTML = '';
    type(render(), '');
    expect(changes).toEqual(['']);
  });

  it('picks from the list with the keyboard, starting at its own time', async () => {
    const input = render();
    input.focus();

    press(input, 'ArrowDown');
    await settle();
    expect(input.getAttribute('aria-expanded')).toBe('true');
    expect(list().querySelectorAll('[role="option"]')).toHaveLength(48);
    expect(spaced(activeOption(input).textContent)).toBe('9:00 AM');

    press(input, 'ArrowDown');
    expect(spaced(activeOption(input).textContent)).toBe('9:30 AM');

    const enter = press(input, 'Enter');
    expect(enter.defaultPrevented).toBe(true);
    expect(changes).toEqual(['09:30']);
    expect(list()).toBeNull();
  });

  it('offers times in the requested step and picks a clicked one', async () => {
    const input = render({ step: 15 });
    input.click();
    await settle();
    expect(list().querySelectorAll('[role="option"]')).toHaveLength(96);
    expect(
      list().querySelector('[aria-selected="true"]').textContent.trim(),
    ).toMatch(/^9:15/);

    const escape = press(input, 'Escape');
    expect(escape.defaultPrevented).toBe(true);
    expect(list()).toBeNull();

    input.click();
    await settle();
    [...list().querySelectorAll('[role="option"]')]
      .find((option) => spaced(option.textContent) === '1:45 PM')
      .click();
    flushSync();
    expect(changes).toEqual(['13:45']);
  });
});
