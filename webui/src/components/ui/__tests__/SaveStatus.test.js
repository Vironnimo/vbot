// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../../lib/i18n.js';
import { reactiveProps } from '../../__tests__/reactiveProps.support.svelte.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: SaveStatus } = await import('../SaveStatus.svelte');

describe('SaveStatus', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    vi.useFakeTimers();
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    vi.useRealTimers();
    document.body.innerHTML = '';
  });

  function render(initial = {}) {
    const props = reactiveProps({ saving: false, pending: false, ...initial });
    mountedComponent = mount(SaveStatus, { target: document.body, props });
    flushSync();
    return props;
  }

  function update(props, changes) {
    Object.assign(props, changes);
    flushSync();
  }

  const saveAction = () => document.querySelector('.save-status button');
  const statusText = () =>
    document.querySelector('[role="status"]').textContent.trim();

  it('shows nothing at rest and a Save action while a draft is unsaved', () => {
    const onClick = vi.fn();
    const props = render({ onClick, type: 'submit' });
    expect(saveAction()).toBeNull();
    expect(statusText()).toBe('');

    update(props, { pending: true });
    expect(saveAction().textContent.trim()).toBe(t('common.save'));
    expect(saveAction().type).toBe('submit');
    saveAction().click();
    expect(onClick).toHaveBeenCalledOnce();

    // A draft that matches the saved state again without a write (an undone
    // edit, a refresh) confirms nothing.
    update(props, { pending: false });
    expect(saveAction()).toBeNull();
    expect(statusText()).toBe('');
  });

  it('reports a finished save briefly, then returns to rest', () => {
    const props = render({ pending: true });

    update(props, { saving: true });
    expect(saveAction()).toBeNull();
    expect(statusText()).toBe(t('common.saving'));

    update(props, { saving: false, pending: false });
    expect(statusText()).toBe(t('common.saved'));

    vi.advanceTimersByTime(1000);
    flushSync();
    expect(statusText()).toBe(t('common.saved'));

    vi.runOnlyPendingTimers();
    flushSync();
    expect(statusText()).toBe('');
    expect(saveAction()).toBeNull();
  });

  it('brings the Save action back after a failed save or newer edits', () => {
    const props = render({ pending: true });

    update(props, { saving: true });
    update(props, { saving: false });
    expect(saveAction()).not.toBeNull();
    expect(statusText()).toBe('');

    update(props, { saving: true });
    update(props, { saving: false, pending: false });
    expect(statusText()).toBe(t('common.saved'));
    update(props, { pending: true });
    expect(saveAction()).not.toBeNull();
    expect(statusText()).toBe('');
  });

  it('keeps keyboard focus in place when the focused Save action goes away', () => {
    const props = render({ pending: true });
    saveAction().focus();

    update(props, { saving: true });

    expect(saveAction()).toBeNull();
    expect(document.activeElement).toBe(
      document.querySelector('[role="status"]'),
    );
  });
});
