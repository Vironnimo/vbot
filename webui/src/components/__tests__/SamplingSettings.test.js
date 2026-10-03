// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: SamplingSettings } =
  await import('../sampling/SamplingSettings.svelte');

describe('SamplingSettings', () => {
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

  function mountSampling(props) {
    mountedComponent = mount(SamplingSettings, {
      target: document.body,
      props,
    });
    flushSync();
  }

  const toggle = () => document.querySelector('#test-sampling-toggle');
  const body = () => document.querySelector('#test-sampling');
  const summary = () =>
    toggle().querySelector('.s-disclosure__meta')?.textContent.trim() ?? '';
  const buttonByText = (text) =>
    [...document.querySelectorAll('button')].find(
      (button) => button.textContent.trim() === text,
    );

  it('stays collapsed with a summary of the custom values until an error opens it', () => {
    const props = reactiveProps({
      idPrefix: 'test',
      fields: { temperature: { value: '0.1' }, top_p: { value: '' } },
      onChange: () => {},
    });
    mountSampling(props);

    expect(toggle().getAttribute('aria-expanded')).toBe('false');
    expect(body().hidden).toBe(true);
    expect(summary()).toBe('Temperature 0.1');

    props.fields = { temperature: { value: '0.1' }, top_p: { value: '0.9' } };
    flushSync();
    expect(summary()).toBe('Temperature 0.1 · Top P 0.9');

    // Open, the rows and the note show the values; the summary steps aside.
    toggle().click();
    flushSync();
    expect(body().hidden).toBe(false);
    expect(summary()).toBe('');
    expect(document.querySelector('#test-sampling-note').textContent).toContain(
      'We recommend the Provider defaults.',
    );
    toggle().click();
    flushSync();

    props.fields = {
      temperature: { value: '0.1' },
      top_p: { value: '2', error: 'Out of range' },
    };
    flushSync();
    expect(toggle().getAttribute('aria-expanded')).toBe('true');
    expect(body().hidden).toBe(false);
    const topP = document.querySelector('#test-top-p');
    expect(topP.getAttribute('aria-invalid')).toBe('true');
    expect(topP.getAttribute('aria-describedby')).toContain('test-top-p-error');
  });

  it('offers a Model recommendation only through its Use action', () => {
    const onChange = vi.fn();
    const onClear = vi.fn();
    mountSampling({
      idPrefix: 'test',
      fields: {
        temperature: {
          value: '',
          hint: 'Inherited: 0.7 (global default)',
          recommended: 0.6,
        },
        top_p: { value: '0,95', recommended: 0.95 },
      },
      onChange,
      onClear,
    });
    toggle().click();
    flushSync();

    // Showing a recommendation never fills the field on its own.
    expect(onChange).not.toHaveBeenCalled();
    expect(document.querySelector('#test-temperature').value).toBe('');
    expect(
      document.querySelector('#test-temperature-help').textContent.trim(),
    ).toBe('Inherited: 0.7 (global default)');
    expect(
      document
        .querySelector('#test-temperature-recommendation')
        .textContent.trim(),
    ).toBe('Model recommends 0.6');
    // A field that already holds the recommendation offers no Use action.
    expect(buttonByText('Use 0.95')).toBeUndefined();

    buttonByText('Use 0.6').click();
    expect(onChange).toHaveBeenCalledExactlyOnceWith('temperature', '0.6');

    // Only a field with its own value offers the clear action.
    const clearButtons = document.querySelectorAll('.sampling-settings__clear');
    expect(clearButtons).toHaveLength(1);
    clearButtons[0].click();
    expect(onClear).toHaveBeenCalledExactlyOnceWith('top_p');
  });
});
