// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: TextField } = await import('../TextField.svelte');

describe('TextField', () => {
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
    mountedComponent = mount(TextField, { target: document.body, props });
    flushSync();
    return document.body;
  }

  it('renders the default input with the s-input class', () => {
    const input = render({ value: 'hi' }).querySelector('input');
    expect(input).toBeTruthy();
    expect(input.classList.contains('s-input')).toBe(true);
    expect(input.value).toBe('hi');
  });

  it('uses the modal-input class for the modal variant', () => {
    const input = render({ variant: 'modal' }).querySelector('input');
    expect(input.classList.contains('modal-input')).toBe(true);
    expect(input.classList.contains('s-input')).toBe(false);
  });

  it('marks invalid with the class and aria-invalid', () => {
    const input = render({ invalid: true }).querySelector('input');
    expect(input.classList.contains('s-input--invalid')).toBe(true);
    expect(input.getAttribute('aria-invalid')).toBe('true');
  });

  it('renders a read-only value-box instead of an input', () => {
    const body = render({ readonly: true, value: '/home/data' });
    expect(body.querySelector('input')).toBeNull();
    const box = body.querySelector('.s-value-box');
    expect(box).toBeTruthy();
    expect(box.textContent).toContain('/home/data');
  });

  it('disables the input', () => {
    const input = render({ disabled: true }).querySelector('input');
    expect(input.disabled).toBe(true);
  });

  it('calls onInput with the new value on input', () => {
    const onInput = vi.fn();
    const input = render({ value: '', onInput }).querySelector('input');
    input.value = 'typed';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    expect(onInput).toHaveBeenCalledTimes(1);
    expect(onInput.mock.calls[0][0]).toBe('typed');
  });

  it('forwards placeholder, inputmode, and a passthrough class', () => {
    const input = render({
      placeholder: 'C:/path',
      inputmode: 'decimal',
      class: 'mono',
    }).querySelector('input');
    expect(input.getAttribute('placeholder')).toBe('C:/path');
    expect(input.getAttribute('inputmode')).toBe('decimal');
    expect(input.classList.contains('s-input')).toBe(true);
    expect(input.classList.contains('mono')).toBe(true);
  });
});
