// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: Toggle } = await import('../Toggle.svelte');

describe('Toggle', () => {
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

  function render(props) {
    if (mountedComponent) unmount(mountedComponent);
    document.body.innerHTML = '';
    mountedComponent = mount(Toggle, { target: document.body, props });
    flushSync();
    return document.body.querySelector('button');
  }

  it('renders a role=switch button with the knob reflecting the checked state', () => {
    const on = render({ checked: true });
    expect(on.getAttribute('role')).toBe('switch');
    expect(on.querySelector('.t-knob')).toBeTruthy();
    expect(on.getAttribute('aria-checked')).toBe('true');
    expect(on.classList.contains('on')).toBe(true);

    const off = render({ checked: false });
    expect(off.getAttribute('aria-checked')).toBe('false');
    expect(off.classList.contains('on')).toBe(false);
  });

  it('uses the large class by default and for size lg, the small class for size sm, and appends passthrough props', () => {
    expect(render({ size: 'lg' }).classList.contains('toggle')).toBe(true);

    const small = render({ size: 'sm' });
    expect(small.classList.contains('tl-toggle')).toBe(true);
    expect(small.classList.contains('toggle')).toBe(false);

    const button = render({
      class: 'agents-view__prompt-toggle',
      ariaLabel: 'Custom prompt',
    });
    expect(button.classList.contains('tl-toggle')).toBe(false);
    expect(button.classList.contains('toggle')).toBe(true);
    expect(button.classList.contains('agents-view__prompt-toggle')).toBe(true);
    expect(button.getAttribute('aria-label')).toBe('Custom prompt');
  });

  it('calls onChange with the toggled value on click, but not while disabled', () => {
    const onChange = vi.fn();
    render({ checked: false, onChange }).click();
    expect(onChange).toHaveBeenLastCalledWith(true);

    render({ checked: true, onChange }).click();
    expect(onChange).toHaveBeenLastCalledWith(false);

    const disabled = render({ disabled: true, onChange });
    expect(disabled.disabled).toBe(true);
    disabled.click();
    expect(onChange).toHaveBeenCalledTimes(2);
  });
});
