// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import { t } from '../../lib/i18n.js';
import {
  chooseSuggestion,
  composerInput,
  deferred,
  getDraft,
  modelCatalogFixture,
  pressKey,
  setDraft,
  settle,
  setupChatComposerSuite,
  skillFixtures,
  submitComposer,
  suggestionNames,
  suggestionOptions,
  typeInComposer,
} from './ChatComposer.support.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const statusCommand = {
  name: 'status',
  description: 'Show current session and runtime status.',
  type: 'command',
  argument: 'none',
};

const selectedIndex = () =>
  suggestionOptions().findIndex(
    (option) => option.getAttribute('aria-selected') === 'true',
  );

describe('ChatComposer suggestions', () => {
  const composer = setupChatComposerSuite();

  describe('commands and skills', () => {
    it.each([
      ['a slash skill', skillFixtures(), '/deb', '/debugging', null],
      [
        'a command named with its slash',
        [{ name: '/compact', description: 'Compact.', type: 'command' }],
        '/com',
        '/compact',
        null,
      ],
      [
        'an argument-bearing command',
        [
          {
            name: 'compact',
            description: 'Compact.',
            type: 'command',
            argument: 'optional',
          },
        ],
        '/com',
        '/compact',
        null,
      ],
      [
        'a loadable skill with validation warnings',
        [
          ...skillFixtures(),
          {
            name: 'warning-skill',
            description: 'Loadable with validation warnings.',
            valid: false,
            warnings: ['Skill name differs from directory name.'],
          },
        ],
        '$warning',
        '$warning-skill',
        null,
      ],
      ['a no-argument command', [statusCommand], '/stat', '', '/status'],
    ])(
      'inserts or runs %s',
      async (_case, availableSkills, typed, inserted, sent) => {
        const onSendMessage = vi.fn().mockResolvedValue(true);
        composer.mount({ availableSkills, onSendMessage });

        typeInComposer(typed);
        expect(suggestionOptions()).toHaveLength(1);
        await chooseSuggestion();
        await settle();

        expect(composerInput().value).toBe(inserted);
        if (sent) {
          expect(onSendMessage).toHaveBeenCalledWith(sent);
        } else {
          expect(onSendMessage).not.toHaveBeenCalled();
        }
      },
    );

    it('offers only skills for an inline dollar trigger and inserts it in place', async () => {
      const onSendMessage = vi.fn().mockResolvedValue(true);
      composer.mount({
        availableSkills: [
          {
            name: 'stop',
            description: 'Cancel the active run.',
            type: 'command',
          },
          ...skillFixtures(),
        ],
        onSendMessage,
      });

      typeInComposer('Please use $', 12);
      expect(suggestionNames()).toEqual(['debugging', 'frontend-design']);
      expect(
        document.body.querySelector('.skill-autocomplete__eyebrow').textContent,
      ).toContain(t('skillAutocomplete.eyebrow.skills'));

      typeInComposer('Please use $deb here.  ', 15);
      pressKey('Enter', { keyup: false });
      await settle();
      expect(composerInput().value).toBe('Please use $debugging here.  ');

      pressKey('Enter', { keyup: false });
      expect(onSendMessage).toHaveBeenCalledWith(
        'Please use $debugging here.  ',
      );
    });

    it('keeps keyboard navigation through every match across key releases', () => {
      composer.mount({
        availableSkills: Array.from({ length: 9 }, (_item, index) => ({
          name: `skill-${index + 1}`,
          description: `Skill number ${index + 1}.`,
          valid: true,
        })),
      });

      typeInComposer('/');
      // All nine render in the scrollable popup and stay reachable.
      expect(suggestionOptions()).toHaveLength(9);
      expect(selectedIndex()).toBe(0);
      for (let step = 0; step < 8; step += 1) pressKey('ArrowDown');
      expect(selectedIndex()).toBe(8);
      pressKey('ArrowUp');
      expect(selectedIndex()).toBe(7);
    });

    it.each([
      ['Escape closes it', 'Escape', '/deb', 4, '/deb'],
      ['Enter chooses a slash match', 'Enter', '/deb', 4, '/debugging'],
      [
        'Enter chooses an inline match',
        'Enter',
        'use $deb here',
        8,
        'use $debugging here',
      ],
    ])(
      'keeps the popup closed after the key release when %s',
      async (_case, key, typed, caret, value) => {
        composer.mount({ availableSkills: skillFixtures() });
        typeInComposer(typed, caret);
        expect(suggestionOptions()).toHaveLength(1);

        pressKey(key, { keyup: false });
        await settle();
        pressKey(key);

        expect(composerInput().value.trim()).toBe(value);
        expect(suggestionOptions()).toHaveLength(0);
      },
    );
  });

  describe('models', () => {
    it('offers tool-capable models after "/model ", filters them, and sends the chosen one', async () => {
      const onSendMessage = vi.fn().mockResolvedValue(true);
      const onLoadModelCatalog = vi
        .fn()
        .mockResolvedValue(modelCatalogFixture());
      composer.mount({ onSendMessage, onLoadModelCatalog });

      typeInComposer('/model ');
      await settle();
      expect(onLoadModelCatalog).toHaveBeenCalledTimes(1);
      expect(
        suggestionOptions('model').map((option) => option.textContent),
      ).toEqual([
        expect.stringContaining('openai/gpt-5.2'),
        expect.stringContaining('anthropic/claude-sonnet-4'),
      ]);

      typeInComposer('/model ant');
      await settle();
      expect(suggestionOptions('model')).toHaveLength(1);

      await chooseSuggestion('model');
      expect(onSendMessage).toHaveBeenCalledWith(
        '/model anthropic/claude-sonnet-4::api-key',
      );
      expect(composerInput().value).toBe('');
    });

    it('does not open the model popup for other slash text', async () => {
      const onLoadModelCatalog = vi
        .fn()
        .mockResolvedValue(modelCatalogFixture());
      composer.mount({ availableSkills: skillFixtures(), onLoadModelCatalog });

      typeInComposer('/modeling something');
      await settle();

      expect(onLoadModelCatalog).not.toHaveBeenCalled();
      expect(document.body.querySelector('.model-autocomplete')).toBeNull();
    });
  });

  it.each([
    ['a no-argument command', 'skill', '/stat'],
    ['a model', 'model', '/model '],
  ])(
    'does not run %s while another send is in flight',
    async (_case, kind, typed) => {
      const send = deferred();
      const onSendMessage = vi.fn(() => send.promise);
      composer.mount({
        availableSkills: [statusCommand],
        onSendMessage,
        onLoadModelCatalog: vi.fn().mockResolvedValue(modelCatalogFixture()),
      });

      typeInComposer('first message');
      submitComposer();
      expect(onSendMessage).toHaveBeenCalledTimes(1);

      typeInComposer(typed);
      await settle();
      await chooseSuggestion(kind);

      expect(onSendMessage).toHaveBeenCalledTimes(1);
      expect(composerInput().value).toBe(typed);
      send.resolve(true);
      await settle();
    },
  );

  describe('file mentions', () => {
    it('opens the file picker on @ and inserts the chosen path', async () => {
      const onListFiles = vi.fn().mockResolvedValue({
        files: ['docs/guide.md', 'src/session_search.py'],
        truncated: false,
      });
      composer.mount({ onListFiles });

      typeInComposer('look at @search');
      await settle();
      expect(onListFiles).toHaveBeenCalledTimes(1);
      expect(
        suggestionOptions('file').map((option) => option.textContent),
      ).toEqual([expect.stringContaining('session_search.py')]);

      await chooseSuggestion('file');
      expect(composerInput().value).toBe('look at @src/session_search.py ');
    });

    it('sends picked paths with spaces or symbols as file mentions', async () => {
      const onSendMessage = vi.fn().mockResolvedValue(true);
      const onListFiles = vi.fn().mockResolvedValue({
        files: ['notes/meeting notes.md', 'docs/Übersicht.md'],
        truncated: false,
      });
      composer.mount({ onSendMessage, onListFiles });

      for (const query of ['meeting', 'Übers']) {
        typeInComposer(`${composerInput().value}@${query}`);
        await settle();
        await chooseSuggestion('file');
      }
      expect(composerInput().value).toBe(
        '@"notes/meeting notes.md" @docs/Übersicht.md ',
      );

      submitComposer();
      await settle(2);
      expect(onSendMessage).toHaveBeenCalledWith(
        '@"notes/meeting notes.md" @docs/Übersicht.md ',
        { fileMentions: ['notes/meeting notes.md', 'docs/Übersicht.md'] },
      );
    });

    it('checks mentions against a changed file listing', async () => {
      const onSendMessage = vi.fn().mockResolvedValue(true);
      const listing = (files) =>
        vi.fn().mockResolvedValue({ files, truncated: false });
      const props = reactiveProps({
        onSendMessage,
        onListFiles: listing(['vbot.md']),
      });
      composer.mount(props);
      typeInComposer('@vb');
      await settle();

      // Another draft Project lists other files.
      props.onListFiles = listing(['docs.md']);
      typeInComposer('see @docs.md now');
      submitComposer();
      await settle(2);

      expect(props.onListFiles).toHaveBeenCalledTimes(1);
      expect(onSendMessage).toHaveBeenCalledWith('see @docs.md now', {
        fileMentions: ['docs.md'],
      });
    });

    it('does not open the file picker inside an email address', async () => {
      const onListFiles = vi.fn().mockResolvedValue({ files: ['a.txt'] });
      composer.mount({ onListFiles });

      typeInComposer('mail user@example');
      await settle();

      expect(onListFiles).not.toHaveBeenCalled();
      expect(document.body.querySelector('.file-autocomplete')).toBeNull();
    });

    it.each([
      [
        'names the typed @-tokens that are real files',
        ['notes.md'],
        'check @notes.md and @nofile.txt',
        [{ fileMentions: ['notes.md'] }],
      ],
      ['sends no options without a real file', [], 'ping @nobody', []],
    ])('%s when sending', async (_case, files, typed, options) => {
      const onSendMessage = vi.fn().mockResolvedValue(true);
      const onListFiles = vi
        .fn()
        .mockResolvedValue({ files, truncated: false });
      composer.mount({ onSendMessage, onListFiles });

      typeInComposer(typed);
      submitComposer();
      await settle(2);

      expect(onSendMessage).toHaveBeenCalledWith(typed, ...options);
      await vi.waitFor(() => expect(composerInput().value).toBe(''));
    });

    it('serializes mention submits and sends the original snapshot', async () => {
      const files = deferred();
      const send = deferred();
      const onListFiles = vi.fn(() => files.promise);
      const onSendMessage = vi.fn(() => send.promise);
      setDraft('agent::one', 'first @notes.md');
      composer.mount({
        draftKey: 'agent::one',
        historyKey: 'agent',
        onSendMessage,
        onListFiles,
      });

      submitComposer();
      submitComposer();
      expect(onListFiles).toHaveBeenCalledTimes(1);
      typeInComposer('second draft');
      files.resolve({ files: ['notes.md'], truncated: false });
      await vi.waitFor(() => expect(onSendMessage).toHaveBeenCalledTimes(1));
      expect(onSendMessage).toHaveBeenCalledWith('first @notes.md', {
        fileMentions: ['notes.md'],
      });

      submitComposer();
      expect(onSendMessage).toHaveBeenCalledTimes(1);
      send.resolve(true);
      await settle();

      expect(composerInput().value).toBe('second draft');
      expect(getDraft('agent::one')).toBe('second draft');
    });
  });

  describe('Enter while a picker loads', () => {
    it('waits for the @ picker and then chooses its match', async () => {
      const onSendMessage = vi.fn().mockResolvedValue(true);
      const files = deferred();
      composer.mount({
        onSendMessage,
        onListFiles: vi.fn(() => files.promise),
      });

      typeInComposer('look at @src/ap');
      const waiting = pressKey('Enter', { keyup: false });
      await settle();
      expect(waiting.defaultPrevented).toBe(true);
      expect(onSendMessage).not.toHaveBeenCalled();
      expect(composerInput().value).toBe('look at @src/ap');

      files.resolve({ files: ['src/app.js'], truncated: false });
      await settle();
      pressKey('Enter', { keyup: false });
      await settle();
      expect(onSendMessage).not.toHaveBeenCalled();
      expect(composerInput().value).toBe('look at @src/app.js ');
    });

    it('waits for the /model catalog and then sends its active option', async () => {
      const onSendMessage = vi.fn().mockResolvedValue(true);
      const catalog = deferred();
      composer.mount({
        onSendMessage,
        onLoadModelCatalog: vi.fn(() => catalog.promise),
      });

      typeInComposer('/model ');
      pressKey('Enter', { keyup: false });
      await settle();
      expect(onSendMessage).not.toHaveBeenCalled();
      expect(composerInput().value).toBe('/model ');

      catalog.resolve(modelCatalogFixture());
      await settle();
      pressKey('Enter', { keyup: false });
      await settle();
      expect(onSendMessage).toHaveBeenCalledWith(
        '/model openai/gpt-5.2::api-key',
      );
    });
  });
});
