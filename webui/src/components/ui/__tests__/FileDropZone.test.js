// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init, t } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: FileDropZone } = await import('../FileDropZone.svelte');

function drop(target, file) {
  const event = new Event('drop', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'dataTransfer', {
    value: { types: ['Files'], files: [file] },
  });
  target.dispatchEvent(event);
  flushSync();
  return event;
}

describe('FileDropZone', () => {
  let component;
  const onFile = vi.fn();

  beforeEach(() => {
    init('en');
    document.body.innerHTML = '';
    onFile.mockReset();
    component = mount(FileDropZone, {
      target: document.body,
      props: { accept: '.skill,.zip,image/*', onFile },
    });
    flushSync();
  });

  afterEach(() => {
    unmount(component);
    document.body.innerHTML = '';
  });

  it('passes on a dropped file that matches accept', () => {
    const file = new File(['x'], 'Demo.SKILL');
    const event = drop(document.querySelector('.file-drop'), file);
    expect(event.defaultPrevented).toBe(true);
    expect(onFile).toHaveBeenCalledWith(file);
    expect(document.querySelector('[role="alert"]')).toBeNull();
  });

  it('refuses a dropped file outside accept with a message', () => {
    drop(
      document.querySelector('.file-drop'),
      new File(['x'], 'notes.txt', { type: 'text/plain' }),
    );
    expect(onFile).not.toHaveBeenCalled();
    expect(document.querySelector('[role="alert"]').textContent).toBe(
      t('fileDrop.wrongType', { name: 'notes.txt' }),
    );
  });
});
