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
    mountedComponent = mount(Toggle, { target: document.body, props });
    flushSync();
    return document.body.querySelector('button');
  }

  it('renders a role=switch button with the knob', () => {
    const button = render({});
    expect(button.getAttribute('role')).toBe('switch');
    expect(button.querySelector('.t-knob')).toBeTruthy();
  });

  it('uses the large class for size lg and the small class for size sm', () => {
    expect(render({ size: 'lg' }).classList.contains('toggle')).toBe(true);

    unmount(mountedComponent);
    mountedComponent = null;
    document.body.innerHTML = '';

    const small = render({ size: 'sm' });
    expect(small.classList.contains('tl-toggle')).toBe(true);
    expect(small.classList.contains('toggle')).toBe(false);
  });

  it('reflects the checked state in aria-checked and the on class', () => {
    const on = render({ checked: true });
    expect(on.getAttribute('aria-checked')).toBe('true');
    expect(on.classList.contains('on')).toBe(true);

    unmount(mountedComponent);
    mountedComponent = null;
    document.body.innerHTML = '';

    const off = render({ checked: false });
    expect(off.getAttribute('aria-checked')).toBe('false');
    expect(off.classList.contains('on')).toBe(false);
  });

  it('calls onChange with the toggled value on click', () => {
    const onChange = vi.fn();
    render({ checked: false, onChange }).click();
    expect(onChange).toHaveBeenCalledWith(true);

    unmount(mountedComponent);
    mountedComponent = null;
    document.body.innerHTML = '';

    const onChangeFromOn = vi.fn();
    render({ checked: true, onChange: onChangeFromOn }).click();
    expect(onChangeFromOn).toHaveBeenCalledWith(false);
  });

  it('does not fire onChange while disabled', () => {
    const onChange = vi.fn();
    const button = render({ disabled: true, onChange });
    expect(button.disabled).toBe(true);
    button.click();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('appends a passthrough class and exposes aria-label', () => {
    const button = render({
      class: 'agents-view__prompt-toggle',
      ariaLabel: 'Custom prompt',
    });
    expect(button.classList.contains('tl-toggle')).toBe(false);
    expect(button.classList.contains('toggle')).toBe(true);
    expect(button.classList.contains('agents-view__prompt-toggle')).toBe(true);
    expect(button.getAttribute('aria-label')).toBe('Custom prompt');
  });
});
