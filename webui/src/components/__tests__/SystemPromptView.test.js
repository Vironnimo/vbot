// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { t } from '../../lib/i18n.js';
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
  clickTab,
  setupSystemPromptViewSuite,
} from './SystemPromptView.support.js';

function hasCall(method) {
  return rpcMock.mock.calls.some((call) => call[0] === method);
}

function editBlock(blockId, value) {
  const textarea = blockElement(blockId).querySelector('textarea');
  textarea.value = value;
  textarea.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

async function advanceAutosave() {
  await vi.advanceTimersByTimeAsync(800);
  await Promise.resolve();
  await Promise.resolve();
  flushSync();
}

describe('SystemPromptView blocks', () => {
  const suite = setupSystemPromptViewSuite();

  async function mountView(options = {}, props = {}) {
    rpcMock.mockImplementation(createRpcMock(options));
    suite.mountedComponent = mount(SystemPromptView, {
      target: document.body,
      props,
    });
    flushSync();
    const expectedIds = (options.blocks ?? baseBlocks()).map(
      (block) => block.id,
    );
    await waitForCondition(() => blockIds().length === expectedIds.length, 100);
  }

  it('renders blocks in layout order with editable text blocks and a collapsed data preview', async () => {
    await mountView();

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
    expect(guide.getAttribute('aria-labelledby')).toBe(
      'sp-blocklist-guide-title',
    );
    expect(guide.querySelector('h3')).toBeTruthy();
    expect(
      guide.querySelectorAll('.sp-blocklist-guide__details p'),
    ).toHaveLength(2);

    // Three editable text blocks get a textarea; the data block has none.
    const textareas = document.body.querySelectorAll(
      'textarea.text-area--inset',
    );
    expect(textareas).toHaveLength(3);
    expect(textareas[0].value).toBe('# Intro');

    // The data block renders a read-only presentation whose preview opens on
    // demand.
    const dataBlock = blockElement('data:soul');
    expect(dataBlock.querySelector('textarea')).toBeNull();
    expect(dataBlock.querySelector('.sp-data-block')).toBeTruthy();
    expect(dataBlock.querySelector('.sp-data-preview')).toBeNull();
    dataBlock.querySelector('.sp-data-toggle').click();
    flushSync();
    expect(dataBlock.querySelector('.sp-data-preview').textContent).toContain(
      '<file>SOUL</file>',
    );
  });

  it('opens a block without changing inclusion or losing edits across tabs', async () => {
    await mountView();
    clickTab(t('systemPrompt.tabs.edit'));
    const block = blockElement('core:intro');
    const disclosure = block.querySelector('button[aria-expanded]');
    expect(disclosure.getAttribute('aria-expanded')).toBe('false');
    disclosure.click();
    flushSync();
    expect(disclosure.getAttribute('aria-expanded')).toBe('true');
    expect(lastCall('prompt.set_layout')).toBeUndefined();
    editBlock('core:intro', 'PRESERVED-DRAFT');
    clickTab(t('systemPrompt.tabs.tools'));
    clickTab(t('systemPrompt.tabs.edit'));
    expect(block.querySelector('textarea').value).toBe('PRESERVED-DRAFT');
    expect(disclosure.getAttribute('aria-expanded')).toBe('true');
  });

  it('toggling a block persists the full ordered layout immediately', async () => {
    await mountView();

    const toggle = blockElement('tool:bash').querySelector(
      'button[role="switch"]',
    );
    expect(toggle.getAttribute('aria-checked')).toBe('true');
    toggle.click();
    flushSync();

    await waitForCondition(() => hasCall('prompt.set_layout'), 100);
    const { layout } = lastCall('prompt.set_layout')[1];
    expect(layout.find((entry) => entry.id === 'tool:bash').enabled).toBe(
      false,
    );
    expect(layout.map((entry) => entry.id)).toEqual(blockIds());
  });

  it('autosaves an edited block after the debounce and then refreshes the preview', async () => {
    await mountView();

    vi.useFakeTimers();
    const previewCallsBefore = rpcMock.mock.calls.filter(
      (call) => call[0] === 'prompt.preview',
    ).length;

    editBlock('core:intro', 'updated intro');
    expect(document.body.textContent).toContain(
      t('systemPrompt.fragmentEditor.dirtyIndicator'),
    );
    expect(hasCall('prompt.update')).toBe(false);

    await advanceAutosave();
    expect(lastCall('prompt.update')[1]).toMatchObject({
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

  it('reorders via the keyboard, announces the position, and autosaves the moved block by id', async () => {
    await mountView();

    const moveWithKey = async (blockId, key) => {
      const layoutCalls = rpcMock.mock.calls.filter(
        (call) => call[0] === 'prompt.set_layout',
      ).length;
      const handle = blockHandle(blockId);
      handle.focus();
      pressKey(handle, key);
      flushSync();
      await waitForCondition(
        () =>
          rpcMock.mock.calls.filter((call) => call[0] === 'prompt.set_layout')
            .length > layoutCalls,
        100,
      );
    };
    await moveWithKey('core:intro', 'ArrowDown');
    await moveWithKey('tool:bash', 'ArrowUp');

    const movedOrder = [
      'memory:guidance',
      'tool:bash',
      'core:intro',
      'data:soul',
    ];
    expect(blockIds()).toEqual(movedOrder);
    expect(
      lastCall('prompt.set_layout')[1].layout.map((entry) => entry.id),
    ).toEqual(movedOrder);
    expect(
      document.body.querySelector('[aria-live="polite"]').textContent,
    ).toContain(
      t('systemPrompt.blockList.reorderAnnouncement', {
        position: 2,
        total: 4,
      }),
    );

    // The autosave targets core:intro by id even though its index changed.
    vi.useFakeTimers();
    editBlock('core:intro', 'edited after move');
    await advanceAutosave();
    expect(lastCall('prompt.update')[1]).toMatchObject({
      id: 'core:intro',
      content: 'edited after move',
    });
  });

  it('reorders via native drag-and-drop and persists the new order', async () => {
    await mountView();

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

    await waitForCondition(() => hasCall('prompt.set_layout'), 100);
    const movedOrder = [
      'memory:guidance',
      'tool:bash',
      'core:intro',
      'data:soul',
    ];
    expect(blockIds()).toEqual(movedOrder);
    expect(
      lastCall('prompt.set_layout')[1].layout.map((entry) => entry.id),
    ).toEqual(movedOrder);
  });

  it('resets a block through prompt.reset after confirmation', async () => {
    await mountView({
      promptReset: {
        id: 'core:intro',
        text: '# Bundled intro',
        is_modified: false,
      },
    });

    Array.from(
      blockElement('core:intro').querySelectorAll('button.btn-secondary'),
    )
      .find(
        (button) =>
          button.textContent.trim() === t('systemPrompt.fragmentEditor.reset'),
      )
      .click();
    flushSync();
    expect(hasCall('prompt.reset')).toBe(false);

    confirmDialog(t('common.reset'));
    flushSync();
    await waitForCondition(() => hasCall('prompt.reset'), 100);
    expect(lastCall('prompt.reset')[1]).toMatchObject({ id: 'core:intro' });
    await waitForCondition(
      () =>
        blockElement('core:intro').querySelector('textarea').value ===
        '# Bundled intro',
      50,
    );
  });

  it.each([
    ['a valid slug', 'my-note', null, true, false],
    ['an invalid slug', '1 bad slug!', null, false, true],
    [
      'a slug the backend rejects',
      'taken',
      new Error('invalid_request'),
      true,
      true,
    ],
  ])(
    'creates a custom block from %s',
    async (_case, slug, createBlockError, expectCreate, expectToast) => {
      const onToast = vi.fn();
      window.prompt = vi.fn(() => slug);
      await mountView({ createBlockError }, { onToast });

      clickToolbarButton(t('systemPrompt.blockList.newBlock'));
      flushSync();
      await Promise.resolve();
      await new Promise((resolve) => setTimeout(resolve, 0));
      flushSync();

      if (expectCreate) {
        await waitForCondition(() => hasCall('prompt.create_block'), 100);
        expect(lastCall('prompt.create_block')[1]).toMatchObject({ slug });
      } else {
        expect(hasCall('prompt.create_block')).toBe(false);
      }
      if (expectToast) {
        await waitForCondition(() => onToast.mock.calls.length > 0, 100);
        expect(onToast).toHaveBeenCalledWith(
          expect.objectContaining({ variant: 'error' }),
        );
      } else {
        expect(onToast).not.toHaveBeenCalled();
      }
    },
  );

  it('removes a custom block through prompt.remove_block after confirmation', async () => {
    await mountView({
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
    });

    const removeLabel = t('common.remove');
    Array.from(
      blockElement('user:my-note').querySelectorAll('button.btn-danger'),
    )
      .find((button) => button.textContent.trim() === removeLabel)
      .click();
    flushSync();
    expect(hasCall('prompt.remove_block')).toBe(false);

    confirmDialog(removeLabel);
    flushSync();
    await waitForCondition(() => hasCall('prompt.remove_block'), 100);
    expect(lastCall('prompt.remove_block')[1]).toMatchObject({
      id: 'user:my-note',
    });
  });

  it('resets the layout through prompt.reset_layout after confirmation', async () => {
    await mountView();

    clickToolbarButton(t('systemPrompt.blockList.resetLayout'));
    flushSync();
    expect(hasCall('prompt.reset_layout')).toBe(false);

    confirmDialog(t('common.reset'));
    flushSync();
    await waitForCondition(() => hasCall('prompt.reset_layout'), 100);
  });
});
