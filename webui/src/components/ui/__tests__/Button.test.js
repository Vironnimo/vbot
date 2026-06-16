// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: Button } = await import('../Button.svelte');

function labelSnippet(text) {
  return createRawSnippet(() => ({
    render: () => `<span>${text}</span>`,
  }));
}

describe('Button', () => {
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
    mountedComponent = mount(Button, { target: document.body, props });
    flushSync();
    return document.body.querySelector('button');
  }

  it('emits the canonical class for each variant', () => {
    const cases = {
      primary: 'btn-primary',
      secondary: 'btn-secondary',
      tertiary: 'btn-tertiary',
      danger: 'btn-danger',
    };

    for (const [variant, expectedClass] of Object.entries(cases)) {
      const button = render({ variant });
      expect(button.classList.contains(expectedClass)).toBe(true);
      unmount(mountedComponent);
      mountedComponent = null;
      document.body.innerHTML = '';
    }
  });

  it('falls back to the secondary class for an unknown variant', () => {
    const button = render({ variant: 'nonsense' });
    expect(button.classList.contains('btn-secondary')).toBe(true);
  });

  it('adds the icon footprint modifier only when icon is set', () => {
    const iconButton = render({ variant: 'primary', icon: true });
    expect(iconButton.classList.contains('btn-icon')).toBe(true);

    unmount(mountedComponent);
    mountedComponent = null;
    document.body.innerHTML = '';

    const plainButton = render({ variant: 'primary' });
    expect(plainButton.classList.contains('btn-icon')).toBe(false);
  });

  it('appends caller-supplied passthrough classes', () => {
    const button = render({ variant: 'secondary', class: 'extra-layout' });
    expect(button.classList.contains('btn-secondary')).toBe(true);
    expect(button.classList.contains('extra-layout')).toBe(true);
  });

  it('defaults the native type to button and honors an override', () => {
    expect(render({}).getAttribute('type')).toBe('button');

    unmount(mountedComponent);
    mountedComponent = null;
    document.body.innerHTML = '';

    expect(render({ type: 'submit' }).getAttribute('type')).toBe('submit');
  });

  it('disables and marks busy while loading', () => {
    const button = render({ loading: true });
    expect(button.disabled).toBe(true);
    expect(button.getAttribute('aria-busy')).toBe('true');
  });

  it('disables on the disabled prop without setting aria-busy', () => {
    const button = render({ disabled: true });
    expect(button.disabled).toBe(true);
    expect(button.getAttribute('aria-busy')).toBe(null);
  });

  it('invokes onClick on click but not while disabled', () => {
    const onClick = vi.fn();
    const button = render({ onClick });
    button.click();
    expect(onClick).toHaveBeenCalledTimes(1);

    unmount(mountedComponent);
    mountedComponent = null;
    document.body.innerHTML = '';

    const disabledClick = vi.fn();
    const disabledButton = render({ onClick: disabledClick, disabled: true });
    disabledButton.click();
    expect(disabledClick).not.toHaveBeenCalled();
  });

  it('exposes aria-label and renders label content', () => {
    const button = render({
      ariaLabel: 'Save changes',
      children: labelSnippet('Save'),
    });
    expect(button.getAttribute('aria-label')).toBe('Save changes');
    expect(button.textContent).toContain('Save');
  });
});
