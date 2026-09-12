// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  mount,
  rpcMock,
  SystemPromptView,
  baseBlocks,
  createRpcMock,
  blockIds,
  blockElement,
  blockHandle,
  clickToolbarButton,
  confirmDialog,
  lastCall,
  pressKey,
  createDataTransfer,
  dragEvent,
  waitForCondition,
  setupSystemPromptViewSuite,
} from './SystemPromptView.support.js';

describe('SystemPromptView', () => {
  const suite = setupSystemPromptViewSuite();

  it('renders blocks in layout order with the shared view chrome', async () => {
    rpcMock.mockImplementation(createRpcMock());

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => blockIds().length === baseBlocks().length,
      100,
    );

    expect(blockIds()).toEqual([
      'core:intro',
      'memory:guidance',
      'tool:bash',
      'data:soul',
    ]);

    expect(document.querySelectorAll('.sp-block-owner')).toHaveLength(4);
    expect(document.querySelector('.sp-scroll.view-frame')).toBeTruthy();
    expect(document.querySelector('.sp-top .sp-navigation')).toBeTruthy();
    expect(document.querySelector('.sp-header.view-header')).toBeTruthy();
    expect(
      document.querySelector('.sp-blocklist-toolbar.view-toolbar--split'),
    ).toBeTruthy();
    const guide = document.querySelector('.sp-blocklist-guide');
    expect(guide).toBeTruthy();
    expect(guide.getAttribute('aria-labelledby')).toBe(
      'sp-blocklist-guide-title',
    );
    expect(guide.querySelector('h3')).toBeTruthy();
    expect(
      guide.querySelectorAll('.sp-blocklist-guide__details p'),
    ).toHaveLength(2);
  });

  it('renders an editable textarea for text blocks but not for data blocks', async () => {
    rpcMock.mockImplementation(createRpcMock());

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => blockIds().length === baseBlocks().length,
      100,
    );

    // Three editable text blocks → three textareas; the data block has none.
    const textareas = document.body.querySelectorAll(
      'textarea.text-area--inset',
    );
    expect(textareas).toHaveLength(3);
    expect(textareas[0].value).toBe('# Intro');

    // The data block renders the read-only data presentation instead.
    const dataBlock = blockElement('data:soul');
    expect(dataBlock.querySelector('textarea')).toBeNull();
    expect(dataBlock.querySelector('.sp-data-block')).toBeTruthy();
  });

  it('reveals the collapsed data block preview on demand', async () => {
    rpcMock.mockImplementation(createRpcMock());

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => blockIds().length === baseBlocks().length,
      100,
    );

    const dataBlock = blockElement('data:soul');
    expect(dataBlock.querySelector('.sp-data-preview')).toBeNull();

    dataBlock.querySelector('.sp-data-toggle').click();
    flushSync();

    expect(dataBlock.querySelector('.sp-data-preview').textContent).toContain(
      '<file>SOUL</file>',
    );
  });

  it('toggling a block persists immediately via prompt.set_layout', async () => {
    rpcMock.mockImplementation(createRpcMock());

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => blockIds().length === baseBlocks().length,
      100,
    );

    const toggle = blockElement('tool:bash').querySelector(
      'button[role="switch"]',
    );
    expect(toggle.getAttribute('aria-checked')).toBe('true');

    toggle.click();
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.set_layout'),
      100,
    );

    const layoutCall = lastCall('prompt.set_layout');
    const bashEntry = layoutCall[1].layout.find(
      (entry) => entry.id === 'tool:bash',
    );
    expect(bashEntry.enabled).toBe(false);
    // The full ordered layout is sent, not just the toggled block.
    expect(layoutCall[1].layout.map((entry) => entry.id)).toEqual(blockIds());
  });

  it('editing an editable block autosaves via prompt.update after the debounce', async () => {
    rpcMock.mockImplementation(createRpcMock());

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => blockIds().length === baseBlocks().length,
      100,
    );

    vi.useFakeTimers();
    const previewCallsBefore = rpcMock.mock.calls.filter(
      (call) => call[0] === 'prompt.preview',
    ).length;

    const textarea = blockElement('core:intro').querySelector('textarea');
    textarea.value = 'updated intro';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    expect(document.body.textContent).toContain('unsaved');
    expect(rpcMock.mock.calls.some((call) => call[0] === 'prompt.update')).toBe(
      false,
    );

    await vi.advanceTimersByTimeAsync(800);
    await Promise.resolve();
    await Promise.resolve();
    flushSync();

    const updateCall = lastCall('prompt.update');
    expect(updateCall[1]).toMatchObject({
      id: 'core:intro',
      content: 'updated intro',
    });

    await vi.advanceTimersByTimeAsync(100);
    await Promise.resolve();
    flushSync();
    expect(
      rpcMock.mock.calls.filter((call) => call[0] === 'prompt.preview').length,
    ).toBeGreaterThan(previewCallsBefore);
  });

  it('autosave keys by block id, not array index, after a reorder', async () => {
    rpcMock.mockImplementation(createRpcMock());

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => blockIds().length === baseBlocks().length,
      100,
    );

    // Move the first block (core:intro) down via the keyboard, then edit it: the
    // autosave must target core:intro by id even though its index changed.
    const handle = blockHandle('core:intro');
    handle.focus();
    pressKey(handle, 'ArrowDown');
    flushSync();
    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.set_layout'),
      100,
    );

    vi.useFakeTimers();
    const textarea = blockElement('core:intro').querySelector('textarea');
    textarea.value = 'edited after move';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    await vi.advanceTimersByTimeAsync(800);
    await Promise.resolve();
    await Promise.resolve();
    flushSync();

    const updateCall = lastCall('prompt.update');
    expect(updateCall[1]).toMatchObject({
      id: 'core:intro',
      content: 'edited after move',
    });
  });

  it('per-block reset calls prompt.reset after confirm', async () => {
    rpcMock.mockImplementation(
      createRpcMock({
        promptReset: {
          id: 'core:intro',
          text: '# Bundled intro',
          is_modified: false,
        },
      }),
    );

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => blockIds().length === baseBlocks().length,
      100,
    );

    const resetButton = Array.from(
      blockElement('core:intro').querySelectorAll('button.btn-secondary'),
    ).find((button) => button.textContent.trim() === 'Reset');
    resetButton.click();
    flushSync();

    // The ConfirmDialog gates the reset; confirming it fires the RPC.
    confirmDialog('Reset');
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.reset'),
      100,
    );

    expect(lastCall('prompt.reset')[1]).toMatchObject({ id: 'core:intro' });

    await waitForCondition(
      () =>
        blockElement('core:intro').querySelector('textarea').value ===
        '# Bundled intro',
      50,
    );
  });

  it('reorders via native drag-and-drop and persists the new order', async () => {
    rpcMock.mockImplementation(createRpcMock());

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => blockIds().length === baseBlocks().length,
      100,
    );

    const dataTransfer = createDataTransfer();
    // Drag the first handle (core:intro) onto the third row (tool:bash).
    blockHandle('core:intro').dispatchEvent(
      dragEvent('dragstart', dataTransfer),
    );
    flushSync();
    blockElement('tool:bash').dispatchEvent(
      dragEvent('dragover', dataTransfer),
    );
    flushSync();
    blockElement('tool:bash').dispatchEvent(dragEvent('drop', dataTransfer));
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.set_layout'),
      100,
    );

    // core:intro moved from index 0 to index 2 (where tool:bash was).
    expect(blockIds()).toEqual([
      'memory:guidance',
      'tool:bash',
      'core:intro',
      'data:soul',
    ]);
    expect(lastCall('prompt.set_layout')[1].layout.map((e) => e.id)).toEqual([
      'memory:guidance',
      'tool:bash',
      'core:intro',
      'data:soul',
    ]);
  });

  it('reorders via the keyboard, persists, and announces the new position', async () => {
    rpcMock.mockImplementation(createRpcMock());

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => blockIds().length === baseBlocks().length,
      100,
    );

    const handle = blockHandle('memory:guidance');
    handle.focus();
    pressKey(handle, 'ArrowUp');
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.set_layout'),
      100,
    );

    // memory:guidance moved up above core:intro.
    expect(blockIds()).toEqual([
      'memory:guidance',
      'core:intro',
      'tool:bash',
      'data:soul',
    ]);
    expect(lastCall('prompt.set_layout')[1].layout[0].id).toBe(
      'memory:guidance',
    );

    // The aria-live region announces the new position.
    const live = document.body.querySelector('[aria-live="polite"]');
    expect(live.textContent).toContain('position 1');
  });

  it('creates a custom block through prompt.create_block', async () => {
    window.prompt = vi.fn(() => 'my-note');
    rpcMock.mockImplementation(createRpcMock());

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => blockIds().length === baseBlocks().length,
      100,
    );

    clickToolbarButton('New block');
    flushSync();

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some((call) => call[0] === 'prompt.create_block'),
      100,
    );

    expect(lastCall('prompt.create_block')[1]).toMatchObject({
      slug: 'my-note',
    });
  });

  it('surfaces a bad slug as a toast without calling the backend', async () => {
    const toastMock = vi.fn();
    window.prompt = vi.fn(() => '1 bad slug!');
    rpcMock.mockImplementation(createRpcMock());

    suite.mountedComponent = mount(SystemPromptView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();

    await waitForCondition(
      () => blockIds().length === baseBlocks().length,
      100,
    );

    clickToolbarButton('New block');
    flushSync();

    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'error' }),
    );
    expect(
      rpcMock.mock.calls.some((call) => call[0] === 'prompt.create_block'),
    ).toBe(false);
  });

  it('surfaces a backend bad-slug rejection as a toast', async () => {
    const toastMock = vi.fn();
    window.prompt = vi.fn(() => 'taken');
    rpcMock.mockImplementation(
      createRpcMock({
        createBlockError: new Error('invalid_request'),
      }),
    );

    suite.mountedComponent = mount(SystemPromptView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();

    await waitForCondition(
      () => blockIds().length === baseBlocks().length,
      100,
    );

    clickToolbarButton('New block');
    flushSync();

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some((call) => call[0] === 'prompt.create_block'),
      100,
    );

    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'error' }),
    );
  });

  it('removes a custom block through prompt.remove_block after confirm', async () => {
    rpcMock.mockImplementation(
      createRpcMock({
        blocks: [
          ...baseBlocks(),
          {
            id: 'user:my-note',
            owner: 'always',
            kind: 'text',
            source: 'user',
            editable: true,
            enabled: true,
            text: 'custom text',
            is_modified: true,
          },
        ],
      }),
    );

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(() => blockIds().includes('user:my-note'), 100);

    const removeButton = Array.from(
      blockElement('user:my-note').querySelectorAll('button.btn-danger'),
    ).find((button) => button.textContent.trim() === 'Remove');
    expect(removeButton).toBeTruthy();

    removeButton.click();
    flushSync();

    // The ConfirmDialog gates the removal; confirming it fires the RPC.
    confirmDialog('Remove');
    flushSync();

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some((call) => call[0] === 'prompt.remove_block'),
      100,
    );

    expect(lastCall('prompt.remove_block')[1]).toMatchObject({
      id: 'user:my-note',
    });
  });

  it('resets the layout through prompt.reset_layout', async () => {
    rpcMock.mockImplementation(createRpcMock());

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => blockIds().length === baseBlocks().length,
      100,
    );

    clickToolbarButton('Reset order & visibility');
    flushSync();

    // The ConfirmDialog gates the layout reset; confirming it fires the RPC.
    confirmDialog('Reset');
    flushSync();

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some((call) => call[0] === 'prompt.reset_layout'),
      100,
    );
  });
});
