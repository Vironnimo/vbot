// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import { t } from '../../lib/i18n.js';
import {
  FLOATING_HOVER_CLOSE_DELAY_MS,
  HOVER_CARD_SHOW_DELAY_MS,
  TOOLTIP_SHOW_DELAY_MS,
} from '../../lib/tooltip.js';
import {
  buttonLabelled,
  composerInput,
  deferred,
  getDraft,
  getHistory,
  pressKey,
  pushHistory,
  selectFilesFromPicker,
  setDraft,
  settle,
  setupChatComposerSuite,
  submitComposer,
  typeInComposer,
  uploadAttachment,
  uploaded,
} from './ChatComposer.support.js';

const attachments = () => document.body.querySelectorAll('.attachment-item');

describe('ChatComposer', () => {
  const composer = setupChatComposerSuite();

  describe('context usage card', () => {
    function mountContextRing(props = {}) {
      composer.mount({
        contextUsage: { tokens: 4000, estimated: true },
        contextWindow: 10000,
        usage: { input_tokens: 3900, output_tokens: 100 },
        compactionState: 'idle',
        onForceCompaction: vi.fn(),
        ...props,
      });
      return {
        anchor: document.querySelector('.context-ring'),
        trigger: document.querySelector('.context-ring-trigger'),
        card: document.querySelector('.context-card'),
      };
    }

    it('shows used context, share, and the usage breakdown', () => {
      const { trigger, card } = mountContextRing();
      trigger.dispatchEvent(new Event('pointerdown', { bubbles: true }));

      expect(card.dataset.floatingOpen).toBe('true');
      expect(card.querySelector('.context-card__title').textContent).toBe(
        t('chat.contextCardTitle'),
      );
      expect(card.querySelector('.context-card__usage').textContent).toBe(
        '4,000 / 10,000',
      );
      expect(card.querySelector('.context-card__percent').textContent).toBe(
        '40%',
      );
      expect(card.querySelector('.context-card__meter-fill').style.width).toBe(
        '40%',
      );
      expect(
        card.querySelector('.context-card__section-title').textContent,
      ).toContain(t('chat.contextCard.lastTurn'));
      expect(card.querySelector('.context-card__level')).toBeNull();
    });

    it('keeps the card open across pointer travel and invokes Compaction', async () => {
      vi.useFakeTimers();
      try {
        const onForceCompaction = vi.fn();
        const { anchor, card } = mountContextRing({ onForceCompaction });
        expect(card.parentElement).toBe(document.body);

        anchor.dispatchEvent(new Event('pointerenter'));
        await vi.advanceTimersByTimeAsync(HOVER_CARD_SHOW_DELAY_MS);
        expect(card.dataset.floatingOpen).toBe('true');
        anchor.dispatchEvent(new Event('pointerleave'));
        card.dispatchEvent(new Event('pointerenter'));
        await vi.advanceTimersByTimeAsync(500);
        expect(card.dataset.floatingOpen).toBe('true');

        const action = card.querySelector('.context-card__action');
        expect(action.textContent.trim()).toBe(t('chat.compactNow'));
        action.click();
        expect(onForceCompaction).toHaveBeenCalledTimes(1);
        window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
        expect(card.dataset.floatingOpen).not.toBe('true');
      } finally {
        vi.useRealTimers();
      }
    });

    it('reaches the action from the ring by keyboard and returns on Escape', () => {
      const { trigger, card } = mountContextRing();
      expect(trigger.tagName).toBe('BUTTON');
      expect(trigger.getAttribute('aria-label')).toBe(
        t('chat.contextRingLabel'),
      );

      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab' }));
      trigger.focus();
      expect(card.dataset.floatingOpen).toBe('true');
      expect(trigger.getAttribute('aria-describedby')).toBe(card.id);

      trigger.dispatchEvent(
        new KeyboardEvent('keydown', {
          key: 'Tab',
          bubbles: true,
          cancelable: true,
        }),
      );
      expect(document.activeElement).toBe(
        card.querySelector('.context-card__action'),
      );

      window.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', cancelable: true }),
      );
      expect(card.dataset.floatingOpen).toBe('false');
      expect(document.activeElement).toBe(trigger);
    });

    const defaultPolicy = {
      enabled: true,
      trigger: { type: 'context_ratio', threshold: 0.8 },
      strategy: { type: 'summary_tail', tail_tokens: 15000 },
    };

    // The thresholds themselves belong to `contextLimitWarning`; the ring and
    // card show its level and note for the displayed Session's Policy.
    it.each([
      ['the default Policy', defaultPolicy, 6900, 'normal', null],
      ['the default Policy', defaultPolicy, 7000, 'high', 'nearLimit'],
      [
        'the default Policy',
        defaultPolicy,
        8000,
        'high',
        'compactionThresholdReached',
      ],
      ['the default Policy', defaultPolicy, 9000, 'critical', 'atLimit'],
      [
        'an earlier threshold',
        {
          ...defaultPolicy,
          trigger: { type: 'context_ratio', threshold: 0.5 },
        },
        4000,
        'high',
        'nearLimit',
      ],
      [
        'disabled automatic Compaction',
        { ...defaultPolicy, enabled: false },
        7000,
        'high',
        'nearContextLimit',
      ],
      ['an unknown Policy', null, 7000, 'high', 'nearContextLimit'],
    ])(
      'relates the context level to %s (%#)',
      (_label, compactionPolicy, tokens, level, noteKey) => {
        const { anchor, card } = mountContextRing({
          contextUsage: { tokens, estimated: false },
          compactionPolicy,
        });

        expect(anchor.classList.contains(`context-ring--${level}`)).toBe(true);
        expect(card.classList.contains(`context-card--${level}`)).toBe(true);
        expect(
          card.querySelector('.context-card__level')?.textContent.trim() ??
            null,
        ).toBe(noteKey ? t(`chat.contextCard.${noteKey}`) : null);
      },
    );

    it.each([
      ['pending', false, 'chat.compactionPending'],
      ['running', false, 'chat.compactionRunning'],
      ['unavailable', false, 'chat.compactNow'],
      ['idle', true, 'chat.compactionPending'],
    ])(
      'disables Compaction while %s (submitting: %s)',
      (compactionState, compactionSubmitting, labelKey) => {
        const onForceCompaction = vi.fn();
        const { card } = mountContextRing({
          compactionState,
          compactionSubmitting,
          onForceCompaction,
        });
        const button = card.querySelector('.context-card__action');
        expect(button.textContent.trim()).toBe(t(labelKey));
        expect(button.disabled).toBe(true);
        button.click();
        expect(onForceCompaction).not.toHaveBeenCalled();
      },
    );

    it('renders the card without an action when none is wired', () => {
      const { card } = mountContextRing({ onForceCompaction: null });
      expect(card.querySelector('.context-card__action')).toBeNull();
    });
  });

  describe('drafts and history', () => {
    const sessionProps = { draftKey: 'agent::one', historyKey: 'agent' };

    it('keeps a per-Session draft and moves it to the history when sent', async () => {
      setDraft('agent::one', 'half a thought');
      const onSendMessage = vi.fn().mockResolvedValue(true);
      composer.mount({ ...sessionProps, onSendMessage });
      expect(composerInput().value).toBe('half a thought');

      typeInComposer('hello there');
      expect(getDraft('agent::one')).toBe('hello there');

      submitComposer();
      await settle();
      expect(onSendMessage).toHaveBeenCalledWith('hello there');
      expect(composerInput().value).toBe('');
      expect(getDraft('agent::one')).toBe('');
      expect(getHistory('agent')).toEqual(['hello there']);
    });

    it.each([
      ['an in-progress', 'my draft'],
      ['an empty', ''],
    ])(
      'recalls sent messages with the arrow keys and returns to %s draft',
      (_case, draft) => {
        pushHistory('agent', 'first');
        pushHistory('agent', 'second');
        composer.mount(sessionProps);
        typeInComposer(draft);

        const recalled = [
          'ArrowUp',
          'ArrowUp',
          'ArrowUp',
          'ArrowDown',
          'ArrowDown',
        ].map((key) => {
          pressKey(key, { keyup: false });
          return composerInput().value;
        });
        // Up holds at the oldest entry; Down past the newest restores the draft.
        expect(recalled).toEqual(['second', 'first', 'first', 'second', draft]);
      },
    );

    it('moves the caret instead of recalling when it is below the first line', () => {
      pushHistory('agent', 'first');
      composer.mount(sessionProps);
      typeInComposer('line one\nline two', 12);

      const event = pressKey('ArrowUp', { keyup: false });
      expect(event.defaultPrevented).toBe(false);
      expect(composerInput().value).toBe('line one\nline two');
    });
  });

  describe('sending', () => {
    it('offers no stop control while nothing runs', () => {
      composer.mount({ isRunning: false, onCancelRun: vi.fn() });
      expect(buttonLabelled('chat.cancelRun')).toBeNull();
      expect(buttonLabelled('chat.stopAll')).toBeNull();
      expect(buttonLabelled('chat.stopOptions')).toBeNull();
    });

    it('offers Stop all alone while only background work runs, also with a disabled composer', () => {
      const onStopAll = vi.fn();
      composer.mount({
        isRunning: false,
        backgroundWorkRunning: true,
        disabled: true,
        onStopAll,
      });

      expect(buttonLabelled('chat.cancelRun')).toBeNull();
      const stopAllButton = buttonLabelled('chat.stopAll');
      expect(stopAllButton.disabled).toBe(false);
      stopAllButton.click();
      expect(onStopAll).toHaveBeenCalledOnce();
    });

    it.each([
      ['a Run is active', { isRunning: true }],
      ['the composer itself is disabled', { isRunning: true, disabled: true }],
    ])('offers the stop control while %s', (_case, props) => {
      const onCancelRun = vi.fn();
      composer.mount({ ...props, onCancelRun });

      const stopButton = buttonLabelled('chat.cancelRun');
      expect(stopButton.disabled).toBe(false);
      stopButton.click();
      expect(onCancelRun).toHaveBeenCalledTimes(1);
    });

    it('disables the stop control while a cancel is in flight', () => {
      composer.mount({ isRunning: true, cancelling: true });
      expect(buttonLabelled('chat.cancelRun')).toBeNull();
      expect(buttonLabelled('cancel.cancelling').disabled).toBe(true);
    });

    it('says why Send is unavailable until there is something to send', async () => {
      vi.useFakeTimers();
      composer.mount();
      const hoverSend = async () => {
        // A disabled button's tooltip lives on its wrapping anchor.
        const button = buttonLabelled('chat.sendMessage');
        const anchor = button.parentElement.matches('.tooltip-anchor')
          ? button.parentElement
          : button;
        anchor.dispatchEvent(new Event('pointerenter'));
        await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
        const text = document.getElementById('app-tooltip').textContent;
        anchor.dispatchEvent(new Event('pointerleave'));
        await vi.advanceTimersByTimeAsync(FLOATING_HOVER_CLOSE_DELAY_MS);
        return text;
      };

      expect(buttonLabelled('chat.sendMessage').disabled).toBe(true);
      expect(await hoverSend()).toBe(t('chat.sendUnavailableEmpty'));

      typeInComposer('Hello');
      expect(buttonLabelled('chat.sendMessage').disabled).toBe(false);
      expect(await hoverSend()).toBe(
        t('chat.sendMessage') + t('chat.sendMessageHint'),
      );
    });

    it('focuses the message field when the composer padding is pressed', () => {
      composer.mount();
      const event = new MouseEvent('mousedown', {
        bubbles: true,
        cancelable: true,
      });
      document.body.querySelector('.input-wrap').dispatchEvent(event);

      expect(event.defaultPrevented).toBe(true);
      expect(document.activeElement).toBe(composerInput());
    });

    it('resets the field height after sending a tall draft', async () => {
      const onSendMessage = vi.fn().mockResolvedValue(true);
      composer.mount({ onSendMessage });
      const input = composerInput();
      Object.defineProperty(input, 'scrollHeight', {
        configurable: true,
        get: () => 144,
      });

      typeInComposer('line one\nline two\nline three');
      expect(input.style.height).toBe('144px');

      submitComposer();
      await settle();
      expect(onSendMessage).toHaveBeenCalledWith(
        'line one\nline two\nline three',
      );
      expect(input.value).toBe('');
      expect(input.style.height).toBe('');
    });

    it.each([
      ['isComposing', { isComposing: true }],
      ['keyCode 229', { keyCode: 229 }],
    ])(
      'leaves an IME-confirming Enter to the composition (%s)',
      async (_label, compositionFlags) => {
        const onSendMessage = vi.fn().mockResolvedValue(true);
        composer.mount({ draftKey: 'a::s1', historyKey: 'a', onSendMessage });
        typeInComposer('にほんご');

        const confirming = pressKey('Enter', {
          keyup: false,
          ...compositionFlags,
        });
        await settle();
        expect(confirming.defaultPrevented).toBe(false);
        expect(onSendMessage).not.toHaveBeenCalled();
        expect(composerInput().value).toBe('にほんご');

        pressKey('Enter', { keyup: false });
        await settle();
        expect(onSendMessage).toHaveBeenCalledWith('にほんご');
      },
    );

    it.each([
      ['an image', 'photo.png', 'image/png', 'media'],
      ['audio', 'voice.ogg', 'audio/ogg', 'media'],
      ['a text file', 'note.txt', 'text/plain', 'file'],
    ])(
      'sends %s upload as a %s block',
      async (_case, filename, mediaType, blockType) => {
        const onSendMessage = vi.fn().mockResolvedValue(true);
        uploadAttachment.mockResolvedValue(
          uploaded('attachment-1', filename, mediaType),
        );
        composer.mount({ onSendMessage });

        await selectFilesFromPicker(
          new File(['data'], filename, { type: mediaType }),
        );
        submitComposer();

        expect(onSendMessage).toHaveBeenCalledWith([
          {
            type: blockType,
            attachment_id: 'attachment-1',
            filename,
            media_type: mediaType,
          },
        ]);
      },
    );

    it('keeps the draft and attachments when send admission fails', async () => {
      const onSendMessage = vi.fn().mockResolvedValue(false);
      uploadAttachment.mockResolvedValue(
        uploaded('attachment-file-1', 'paper.pdf', 'application/pdf'),
      );
      composer.mount({
        draftKey: 'agent::one',
        historyKey: 'agent',
        onSendMessage,
      });

      typeInComposer('keep this');
      await selectFilesFromPicker(
        new File(['pdf-content'], 'paper.pdf', { type: 'application/pdf' }),
      );
      submitComposer();
      await settle();

      expect(onSendMessage).toHaveBeenCalledWith([
        { type: 'text', text: 'keep this' },
        {
          type: 'file',
          attachment_id: 'attachment-file-1',
          filename: 'paper.pdf',
          media_type: 'application/pdf',
        },
      ]);
      expect(composerInput().value).toBe('keep this');
      expect(getDraft('agent::one')).toBe('keep this');
      expect(attachments()).toHaveLength(1);
      expect(getHistory('agent')).toEqual([]);
    });

    it.each([
      ['finished', false],
      ['still running', true],
    ])(
      'continues what a draft gained during its first send in the created Session (upload %s)',
      async (_upload, uploadOutlastsSend) => {
        const send = deferred();
        const upload = deferred();
        const onSendMessage = vi.fn(() => send.promise);
        const later = uploaded(
          'attachment-file-2',
          'later.pdf',
          'application/pdf',
        );
        uploadAttachment.mockImplementation(() =>
          uploadOutlastsSend ? upload.promise : Promise.resolve(later),
        );
        const draft = { draftKey: 'agent::~draft-0', historyKey: 'agent' };
        composer.mount({ ...draft, onSendMessage });

        typeInComposer('first message');
        submitComposer();
        await settle();
        typeInComposer('follow-up');
        await selectFilesFromPicker(
          new File(['pdf-content'], 'later.pdf', { type: 'application/pdf' }),
        );
        send.resolve({ draftKey: 'agent::created' });
        await settle();
        upload.resolve(later);
        await settle();

        expect(onSendMessage).toHaveBeenCalledWith('first message');
        expect(getHistory('agent')).toEqual(['first message']);
        expect(getDraft('agent::~draft-0')).toBe('');
        expect(getDraft('agent::created')).toBe('follow-up');

        await composer.unmount();
        composer.mount({ ...draft, onSendMessage });
        expect(composerInput().value).toBe('');
        expect(attachments()).toHaveLength(0);

        await composer.unmount();
        composer.mount({ draftKey: 'agent::created', historyKey: 'agent' });
        expect(composerInput().value).toBe('follow-up');
        expect(attachments()).toHaveLength(1);
      },
    );

    it('keeps completed attachments with their original Session across composer mounts', async () => {
      uploadAttachment.mockResolvedValue(
        uploaded('attachment-file-1', 'brief.pdf', 'application/pdf'),
      );
      const first = {
        draftKey: 'agent-one::session-one',
        historyKey: 'agent-one',
      };
      composer.mount(first);
      await selectFilesFromPicker(
        new File(['pdf-content'], 'brief.pdf', { type: 'application/pdf' }),
      );
      expect(attachments()).toHaveLength(1);

      await composer.unmount();
      composer.mount({
        draftKey: 'agent-two::session-two',
        historyKey: 'agent-two',
      });
      expect(attachments()).toHaveLength(0);

      await composer.unmount();
      const onSendMessage = vi.fn().mockResolvedValue(true);
      composer.mount({ ...first, onSendMessage });
      expect(attachments()).toHaveLength(1);
      submitComposer();
      await settle();

      expect(onSendMessage).toHaveBeenCalledWith([
        {
          type: 'file',
          attachment_id: 'attachment-file-1',
          filename: 'brief.pdf',
          media_type: 'application/pdf',
        },
      ]);
    });
  });
});
