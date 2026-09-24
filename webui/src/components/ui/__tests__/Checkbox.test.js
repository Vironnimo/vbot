// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: Checkbox } = await import('../Checkbox.svelte');

function content(text) {
  return createRawSnippet(() => ({ render: () => `<span>${text}</span>` }));
}

describe('Checkbox', () => {
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
    mountedComponent = mount(Checkbox, { target: document.body, props });
    flushSync();
    return document.body.querySelector('button');
  }

  it('reports checked, unchecked and mixed states', () => {
    expect(render({ checked: true }).getAttribute('aria-checked')).toBe('true');
    expect(render({ checked: false }).getAttribute('aria-checked')).toBe(
      'false',
    );
    const mixed = render({ checked: false, indeterminate: true });
    expect(mixed.getAttribute('role')).toBe('checkbox');
    expect(mixed.getAttribute('aria-checked')).toBe('mixed');
  });

  it('calls onChange with the next value, selecting from the mixed state', () => {
    const onChange = vi.fn();
    render({ checked: false, onChange }).click();
    expect(onChange).toHaveBeenLastCalledWith(true);

    render({ checked: true, onChange }).click();
    expect(onChange).toHaveBeenLastCalledWith(false);

    render({ indeterminate: true, onChange }).click();
    expect(onChange).toHaveBeenLastCalledWith(true);
  });

  it('does not fire onChange while disabled', () => {
    const onChange = vi.fn();
    const button = render({ disabled: true, onChange });
    expect(button.disabled).toBe(true);
    button.click();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('makes a passed label part of the click target', () => {
    const onChange = vi.fn();
    const button = render({ children: content('read'), onChange });
    expect(button.classList.contains('checkbox--labelled')).toBe(true);
    expect(button.querySelector('.checkbox__label').textContent).toBe('read');
    button.querySelector('.checkbox__label span').click();
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it('passes class, accessible name and data attributes through', () => {
    const button = render({
      class: 'tool-access-chip',
      ariaLabel: 'All Files Tools',
      'data-tool-family': 'files',
    });
    expect(button.classList.contains('checkbox')).toBe(true);
    expect(button.classList.contains('tool-access-chip')).toBe(true);
    expect(button.classList.contains('checkbox--labelled')).toBe(false);
    expect(button.getAttribute('aria-label')).toBe('All Files Tools');
    expect(button.dataset.toolFamily).toBe('files');
  });
});
