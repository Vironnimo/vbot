// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';
import { reactiveProps } from '../../__tests__/reactiveProps.support.svelte.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: DateField } = await import('../DateField.svelte');

const spaced = (text) => text.replace(/\s/g, ' ');

describe('DateField', () => {
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
      value: '2026-09-23',
      today: '2026-09-10',
      ariaLabel: 'Start date',
      onChange: (next) => {
        changes.push(next);
        props.value = next;
      },
      ...initial,
    });
    mounted = mount(DateField, { target: document.body, props });
    flushSync();
    return document.body.querySelector('input');
  }

  function type(input, text) {
    input.value = text;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
  }

  function press(target, key, options = {}) {
    const event = new KeyboardEvent('keydown', {
      key,
      bubbles: true,
      cancelable: true,
      ...options,
    });
    target.dispatchEvent(event);
    flushSync();
    return event;
  }

  async function settle() {
    for (let round = 0; round < 4; round += 1) await Promise.resolve();
    flushSync();
  }

  const panel = () => document.body.querySelector('.date-field__panel');
  const day = (label) =>
    [...document.body.querySelectorAll('.date-field__day')].find(
      (cell) => cell.getAttribute('aria-label') === label,
    );

  it('shows its date in the app language', () => {
    const input = render();

    expect(spaced(input.value)).toBe('Wed, Sep 23, 2026');
    expect(input.getAttribute('aria-label')).toBe('Start date');
  });

  it.each([
    ['10/4', '2026-10-04'],
    ['10/4/27', '2027-10-04'],
    ['Oct 4', '2026-10-04'],
    ['4 October 2027', '2027-10-04'],
    ['Sun, Oct 4, 2026', '2026-10-04'],
    ['2026-10-04', '2026-10-04'],
    // An empty field takes the year of today in the application zone.
    ['Oct 4', '2027-10-04', { value: '', today: '2027-01-01' }],
  ])('reads the typed date %j as %s', (text, expected, initial = {}) => {
    const input = render(initial);

    type(input, text);

    expect(changes).toEqual([expected]);
  });

  it('keeps its date through text that is no date and shows it again on leaving', () => {
    const input = render({ min: '2026-09-01' });
    input.focus();

    for (const text of ['2/30', 'soon', 'Aug 31']) type(input, text);
    expect(changes).toEqual([]);

    input.blur();
    flushSync();
    expect(spaced(input.value)).toBe('Wed, Sep 23, 2026');
  });

  it('clears only when the date is optional', () => {
    const input = render({ required: true });
    type(input, '');
    expect(changes).toEqual([]);

    unmount(mounted);
    mounted = null;
    document.body.innerHTML = '';
    const optional = render();
    type(optional, '  ');
    expect(changes).toEqual(['']);
  });

  it('picks a day with the keyboard and returns to the field', async () => {
    const input = render({ min: '2026-09-22' });
    input.focus();

    press(input, 'ArrowDown');
    await settle();
    expect(input.getAttribute('aria-expanded')).toBe('true');
    expect(document.activeElement).toBe(day('Wednesday, September 23, 2026'));
    expect(
      day('Wednesday, September 23, 2026').getAttribute('aria-selected'),
    ).toBe('true');
    expect(
      day('Thursday, September 10, 2026').getAttribute('aria-current'),
    ).toBe('date');
    expect(day('Monday, September 21, 2026').disabled).toBe(true);

    press(document.activeElement, 'ArrowRight');
    await settle();
    press(document.activeElement, 'ArrowDown');
    await settle();
    expect(panel().querySelector('.date-field__title').textContent).toBe(
      'October 2026',
    );
    press(document.activeElement, 'PageDown');
    await settle();
    expect(document.activeElement).toBe(day('Sunday, November 1, 2026'));

    press(document.activeElement, 'Enter');
    await settle();
    expect(changes).toEqual(['2026-11-01']);
    expect(panel()).toBeNull();
    expect(document.activeElement).toBe(input);
    expect(spaced(input.value)).toBe('Sun, Nov 1, 2026');
  });

  it('closes on Escape without a change and claims the key', async () => {
    const input = render();
    input.focus();
    press(input, 'ArrowDown');
    await settle();

    const escape = press(document.activeElement, 'Escape');

    expect(escape.defaultPrevented).toBe(true);
    expect(panel()).toBeNull();
    expect(document.activeElement).toBe(input);
    expect(changes).toEqual([]);
  });

  it('picks a clicked day, also in another month', async () => {
    const input = render();
    input.click();
    await settle();
    expect(document.activeElement).not.toBe(
      day('Wednesday, September 23, 2026'),
    );

    panel()
      .querySelector('[aria-label="Next month"]')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    day('Monday, October 12, 2026').click();
    flushSync();

    expect(changes).toEqual(['2026-10-12']);
    expect(panel()).toBeNull();
  });
});
