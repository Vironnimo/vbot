// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  clearDraft,
  flushComposerMemory,
  getDraft,
  getHistory,
  getPendingAttachments,
  pushHistory,
  resetComposerMemory,
  setPendingAttachments,
  setDraft,
} from '../composerMemory.js';

beforeEach(() => {
  localStorage.clear();
  resetComposerMemory();
});

afterEach(() => {
  resetComposerMemory();
  localStorage.clear();
});

describe('composerMemory drafts', () => {
  it('returns an empty draft for an unknown or blank key', () => {
    expect(getDraft('agent::session')).toBe('');
    expect(getDraft('')).toBe('');
  });

  it('stores and reads back a per-session draft', () => {
    setDraft('agent::one', 'half a thought');
    setDraft('agent::two', 'a different thought');

    expect(getDraft('agent::one')).toBe('half a thought');
    expect(getDraft('agent::two')).toBe('a different thought');
  });

  it.each([
    ['set to empty text', () => setDraft('agent::one', '')],
    ['cleared explicitly', () => clearDraft('agent::one')],
  ])('clears a draft %s', (_label, clear) => {
    setDraft('agent::one', 'something');
    clear();

    expect(getDraft('agent::one')).toBe('');
  });

  it('evicts the oldest sessions past the cap', () => {
    for (let index = 0; index < 90; index += 1) {
      setDraft(`agent::s${index}`, `draft ${index}`);
    }

    // The 80-session cap drops the oldest while keeping the most recent.
    expect(getDraft('agent::s0')).toBe('');
    expect(getDraft('agent::s9')).toBe('');
    expect(getDraft('agent::s10')).toBe('draft 10');
    expect(getDraft('agent::s89')).toBe('draft 89');
  });

  it('persists drafts to localStorage on flush', () => {
    setDraft('agent::one', 'keep me');
    flushComposerMemory();

    const stored = JSON.parse(localStorage.getItem('vbot.composer.drafts.v1'));
    expect(stored['agent::one']).toBe('keep me');
  });
});

describe('composerMemory history', () => {
  it('returns an empty history for an unknown or blank key', () => {
    expect(getHistory('agent')).toEqual([]);
    expect(getHistory('')).toEqual([]);
  });

  it.each([
    [
      'records sent messages newest-first',
      ['first', 'second'],
      ['second', 'first'],
    ],
    ['ignores a consecutive duplicate send', ['same', 'same'], ['same']],
    [
      'floats a reused message back to the top without duplicating it',
      ['a', 'b', 'a'],
      ['a', 'b'],
    ],
    ['trims entries and skips blank sends', ['  spaced  ', '   '], ['spaced']],
  ])('%s', (_label, sends, history) => {
    for (const text of sends) {
      pushHistory('agent', text);
    }

    expect(getHistory('agent')).toEqual(history);
  });

  it('keeps history scoped per agent', () => {
    pushHistory('agent-a', 'for a');
    pushHistory('agent-b', 'for b');

    expect(getHistory('agent-a')).toEqual(['for a']);
    expect(getHistory('agent-b')).toEqual(['for b']);
  });

  it('caps history length per agent', () => {
    for (let index = 0; index < 120; index += 1) {
      pushHistory('agent', `message ${index}`);
    }

    const history = getHistory('agent');
    expect(history).toHaveLength(100);
    expect(history[0]).toBe('message 119');
    expect(history[99]).toBe('message 20');
  });
});

describe('composerMemory attachments', () => {
  it('keeps uploaded attachments isolated to their session', () => {
    setPendingAttachments('agent-one::session-one', [
      {
        attachment_id: 'attachment-one',
        filename: 'image1.png',
        media_type: 'image/png',
      },
    ]);

    expect(getPendingAttachments('agent-one::session-one')).toEqual([
      {
        attachment_id: 'attachment-one',
        filename: 'image1.png',
        media_type: 'image/png',
      },
    ]);
    expect(getPendingAttachments('agent-two::session-two')).toEqual([]);
  });

  it('persists only completed attachment metadata', () => {
    setPendingAttachments('agent::session', [
      {
        attachment_id: '',
        filename: 'uploading.png',
        media_type: 'image/png',
        uploading: true,
      },
      {
        attachment_id: 'attachment-one',
        filename: 'image1.png',
        media_type: 'image/png',
        preview_url: 'blob:temporary-preview',
      },
    ]);
    flushComposerMemory();

    expect(
      JSON.parse(localStorage.getItem('vbot.composer.attachments.v1')),
    ).toEqual({
      'agent::session': [
        {
          attachment_id: 'attachment-one',
          filename: 'image1.png',
          media_type: 'image/png',
        },
      ],
    });
  });
});

describe('composerMemory across browser tabs', () => {
  const DRAFTS = 'vbot.composer.drafts.v1';
  const HISTORY = 'vbot.composer.history.v1';
  const ATTACHMENTS = 'vbot.composer.attachments.v1';

  // Each module instance stands for one tab sharing the origin's storage.
  async function openTab() {
    vi.resetModules();
    return import('../composerMemory.js');
  }

  function storedDrafts() {
    return JSON.parse(localStorage.getItem(DRAFTS));
  }

  function announceStorageWrite(key, newValue = localStorage.getItem(key)) {
    window.dispatchEvent(
      new StorageEvent('storage', {
        key,
        newValue,
        storageArea: localStorage,
      }),
    );
  }

  it.each([
    'before removal',
    'before storage event',
    'after storage event',
    'before delayed storage event',
  ])(
    'merges a completed upload %s without restoring a remotely sent attachment',
    async (completion) => {
      const tab = await openTab();
      const key = 'alpha::s1';
      const first = {
        attachment_id: 'first',
        filename: 'first.pdf',
        media_type: 'application/pdf',
      };
      const second = {
        attachment_id: 'second',
        filename: 'second.pdf',
        media_type: 'application/pdf',
      };
      tab.setPendingAttachments(key, [first]);
      tab.flushComposerMemory();
      const finishUpload = () =>
        tab.setPendingAttachments(key, [
          ...tab.getPendingAttachments(key),
          second,
        ]);
      if (completion === 'before removal') finishUpload();
      localStorage.setItem(ATTACHMENTS, '{}');
      if (
        completion === 'before storage event' ||
        completion === 'before delayed storage event'
      )
        finishUpload();
      if (completion === 'before delayed storage event')
        tab.flushComposerMemory();
      announceStorageWrite(ATTACHMENTS, '{}');
      if (completion === 'after storage event') finishUpload();
      expect(tab.getPendingAttachments(key)).toEqual([second]);
      tab.flushComposerMemory();
      expect(JSON.parse(localStorage.getItem(ATTACHMENTS))).toEqual({
        [key]: [second],
      });
    },
  );

  it("keeps the other tab's drafts and history when persisting", async () => {
    const tabA = await openTab();
    const tabB = await openTab();

    tabA.setDraft('alpha::s1', 'long draft typed in tab A');
    tabA.pushHistory('alpha', 'message sent from tab A');
    tabA.flushComposerMemory();
    tabB.setDraft('alpha::s2', 'x');
    tabB.pushHistory('alpha', 'message sent from tab B');
    tabB.flushComposerMemory();

    expect(storedDrafts()).toEqual({
      'alpha::s1': 'long draft typed in tab A',
      'alpha::s2': 'x',
    });
    expect(JSON.parse(localStorage.getItem(HISTORY))).toEqual({
      alpha: ['message sent from tab B', 'message sent from tab A'],
    });
  });

  it('does not resurrect a draft another tab already sent', async () => {
    localStorage.setItem(DRAFTS, JSON.stringify({ 'alpha::s1': 'sent text' }));
    const tabA = await openTab();
    const tabB = await openTab();

    tabA.clearDraft('alpha::s1');
    tabA.flushComposerMemory();
    tabB.setDraft('alpha::s2', 'unrelated');
    tabB.flushComposerMemory();

    expect(storedDrafts()).toEqual({ 'alpha::s2': 'unrelated' });
  });

  it("adopts another tab's writes while keeping unsaved local edits", async () => {
    const tabB = await openTab();
    tabB.setDraft('alpha::local', 'still typing here');
    tabB.pushHistory('alpha', 'local send');

    localStorage.setItem(
      DRAFTS,
      JSON.stringify({ 'alpha::s1': 'from tab A', 'alpha::local': 'older' }),
    );
    localStorage.setItem(HISTORY, JSON.stringify({ alpha: ['remote send'] }));
    announceStorageWrite(DRAFTS);
    announceStorageWrite(HISTORY);

    expect(tabB.getDraft('alpha::s1')).toBe('from tab A');
    expect(tabB.getDraft('alpha::local')).toBe('still typing here');
    expect(tabB.getHistory('alpha')).toEqual(['local send', 'remote send']);

    localStorage.setItem(DRAFTS, JSON.stringify({}));
    announceStorageWrite(DRAFTS);

    expect(tabB.getDraft('alpha::s1')).toBe('');
    expect(tabB.getDraft('alpha::local')).toBe('still typing here');
  });

  it("moves a renamed Agent's composer memory in every tab", async () => {
    localStorage.setItem(HISTORY, JSON.stringify({ gamma: ['former gamma'] }));
    const tabA = await openTab();
    const tabB = await openTab();
    const remoteAttachment = {
      attachment_id: 'remote',
      filename: 'remote.pdf',
      media_type: 'application/pdf',
    };
    const localAttachment = {
      ...remoteAttachment,
      attachment_id: 'local',
      filename: 'local.pdf',
    };
    // The rename also reaches stored attachments whose event has not arrived.
    localStorage.setItem(
      ATTACHMENTS,
      JSON.stringify({ 'alpha::s1': [remoteAttachment] }),
    );
    tabA.setDraft('alpha::s1', 'draft in tab A');
    tabA.pushHistory('alpha', 'sent as alpha');
    tabA.flushComposerMemory();
    tabB.setDraft('alpha::~draft-0', 'unsaved draft in tab B');
    tabB.setDraft('beta::s1', 'other Agent');
    tabB.setPendingAttachments('alpha::~draft-0', [localAttachment]);

    // Every open tab hears of the rename and applies it.
    for (const tab of [tabA, tabB]) {
      tab.renameComposerAgent('alpha', 'gamma');
      tab.flushComposerMemory();
      expect(tab.getPendingAttachments('gamma::s1')).toEqual([
        remoteAttachment,
      ]);
      expect(tab.getPendingAttachments('alpha::s1')).toEqual([]);
    }

    expect(storedDrafts()).toEqual({
      'gamma::s1': 'draft in tab A',
      'gamma::~draft-0': 'unsaved draft in tab B',
      'beta::s1': 'other Agent',
    });
    expect(JSON.parse(localStorage.getItem(HISTORY))).toEqual({
      gamma: ['sent as alpha', 'former gamma'],
    });
    expect(tabB.getDraft('gamma::~draft-0')).toBe('unsaved draft in tab B');
    expect(tabA.getHistory('gamma')).toEqual(['sent as alpha', 'former gamma']);
    expect(JSON.parse(localStorage.getItem(ATTACHMENTS))).toEqual({
      'gamma::s1': [remoteAttachment],
      'gamma::~draft-0': [localAttachment],
    });

    // An empty rename must finish too, before the old id is reused later.
    tabA.renameComposerAgent('empty', 'renamed');
    tabA.flushComposerMemory();
    tabB.setPendingAttachments('empty::reused', [remoteAttachment]);
    tabB.flushComposerMemory();
    tabA.setPendingAttachments('beta::unrelated', [localAttachment]);
    tabA.flushComposerMemory();
    expect(tabA.getPendingAttachments('empty::reused')).toEqual([
      remoteAttachment,
    ]);
    expect(tabA.getPendingAttachments('renamed::reused')).toEqual([]);
    expect(
      JSON.parse(localStorage.getItem(ATTACHMENTS))['empty::reused'],
    ).toEqual([remoteAttachment]);
  });

  it('keeps the merged store bounded to the newest sessions', async () => {
    const seeded = Object.fromEntries(
      Array.from({ length: 80 }, (_, index) => [`alpha::s${index}`, 'draft']),
    );
    localStorage.setItem(DRAFTS, JSON.stringify(seeded));
    const tab = await openTab();

    tab.setDraft('alpha::newest', 'fresh');
    tab.flushComposerMemory();

    const stored = storedDrafts();
    expect(Object.keys(stored)).toHaveLength(80);
    expect(stored['alpha::s0']).toBeUndefined();
    expect(stored['alpha::newest']).toBe('fresh');
  });
});
