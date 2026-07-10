// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: FormField } = await import('../FormField.svelte');

describe('FormField', () => {
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
    let contract;
    const children = createRawSnippet((getContract) => {
      contract = getContract();
      return {
        render: () => '<input id="agent-name" />',
      };
    });

    mountedComponent = mount(FormField, {
      target: document.body,
      props: {
        controlId: 'agent-name',
        label: 'Name',
        children,
        ...props,
      },
    });
    flushSync();
    return { root: document.querySelector('.form-field'), contract };
  }

  it('associates its label with the rendered control', () => {
    const { root, contract } = render();

    expect(root.querySelector('label').htmlFor).toBe('agent-name');
    expect(root.querySelector('label').id).toBe('agent-name-label');
    expect(contract.controlId).toBe('agent-name');
    expect(contract.labelId).toBe('agent-name-label');
  });

  it('provides help and error ids as the control contract', () => {
    const { root, contract } = render({
      help: 'Shown to other users.',
      error: 'Enter a name.',
    });

    expect(contract.describedBy).toBe('agent-name-help agent-name-error');
    expect(contract.invalid).toBe(true);
    expect(root.querySelector('.form-field__help').id).toBe('agent-name-help');
    expect(root.querySelector('.form-field__error').id).toBe(
      'agent-name-error',
    );
    expect(root.querySelector('.form-field__error').getAttribute('role')).toBe(
      'alert',
    );
  });

  it('owns full-width and required presentation', () => {
    const { root } = render({ full: true, required: true });

    expect(root.classList.contains('form-field--full')).toBe(true);
    expect(root.querySelector('.form-field__required').textContent).toBe('*');
  });
});
