// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: TextArea } = await import('../TextArea.svelte');

describe('TextArea', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  function render(props = {}) {
    mountedComponent = mount(TextArea, {
      target: document.body,
      props: { value: 'Initial content', ...props },
    });
    flushSync();
    return document.querySelector('textarea');
  }

  it('renders the default controlled multi-line field', () => {
    const textArea = render({ rows: 4, placeholder: 'Describe the run' });

    expect(textArea.classList.contains('text-area--default')).toBe(true);
    expect(textArea.value).toBe('Initial content');
    expect(textArea.rows).toBe(4);
    expect(textArea.placeholder).toBe('Describe the run');
  });

  it('reports edits with the next value and native event', () => {
    const onInput = vi.fn();
    const textArea = render({ onInput });

    textArea.value = 'Edited content';
    textArea.dispatchEvent(new Event('input', { bubbles: true }));

    expect(onInput).toHaveBeenCalledTimes(1);
    expect(onInput.mock.calls[0][0]).toBe('Edited content');
    expect(onInput.mock.calls[0][1]).toBeInstanceOf(Event);
  });

  it('owns code and invalid states with their ARIA contract', () => {
    const textArea = render({ code: true, invalid: true });

    expect(textArea.classList.contains('text-area--code')).toBe(true);
    expect(textArea.classList.contains('text-area--invalid')).toBe(true);
    expect(textArea.getAttribute('aria-invalid')).toBe('true');
  });

  it('supports the inset editor variant and safe fallback', () => {
    const textArea = render({ variant: 'inset', class: 'extra' });

    expect(textArea.classList.contains('text-area--inset')).toBe(true);
    expect(textArea.classList.contains('extra')).toBe(true);
  });

  it('forwards field state and accessibility attributes', () => {
    const textArea = render({
      disabled: true,
      readonly: true,
      ariaLabel: 'Skill content',
      'aria-describedby': 'skill-help',
      spellcheck: false,
    });

    expect(textArea.disabled).toBe(true);
    expect(textArea.readOnly).toBe(true);
    expect(textArea.getAttribute('aria-label')).toBe('Skill content');
    expect(textArea.getAttribute('aria-describedby')).toBe('skill-help');
    expect(textArea.getAttribute('spellcheck')).toBe('false');
    expect(textArea.getAttribute('aria-invalid')).toBe('false');
  });
});
