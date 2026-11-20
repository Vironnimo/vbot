// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { registerCatalog, t } from '../../lib/i18n.js';
import catalog from '../../../../resources/extensions/homeassistant/ui/i18n.js';
import WakewordCommands from '../../../../resources/extensions/homeassistant/ui/WakewordCommands.svelte';

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
registerCatalog(catalog);
let component;
afterEach(async () => {
  await unmount(component);
  document.body.innerHTML = '';
});

async function settle() {
  for (let i = 0; i < 8; i++) {
    await Promise.resolve();
    flushSync();
  }
}

function button(label) {
  return Array.from(document.querySelectorAll('button')).find(
    (node) => node.textContent.trim() === label,
  );
}

function enter(id, text) {
  const node = document.getElementById(id);
  node.value = text;
  node.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

it('creates a fixed call, preserves newer edits during autosave and removes the selected command', async () => {
  let commands = [];
  let autosave;
  let invalidate;
  let gate = null;
  const operation = vi.fn(async (name, change) => {
    if (name === 'set_wakeword_command') {
      if (gate) await gate;
      if (change.previous) expect(change.previous).toEqual(commands[0]);
      commands = change.command ? [change.command] : [];
    } else expect(name).toBe('wakeword_commands');
    return { commands: structuredClone(commands), decision_command_limit: 254 };
  });
  const unsub = () => () => {};
  const bridgeClient = {
    operation,
    context: { locale: 'en' },
    onContext: unsub,
    onInvalidation: (listener) => {
      invalidate = listener;
      return () => {};
    },
    onSearch: unsub,
    registerLayer: unsub,
    setSearchIndex: vi.fn().mockResolvedValue({}),
    notifyAutosave: vi.fn(),
    registerAutosave: (participant) => {
      autosave = participant;
      return () => {};
    },
    dispose: vi.fn(),
  };
  component = mount(WakewordCommands, {
    target: document.body,
    props: { bridgeClient },
  });
  await settle();
  button(t('haWakeword.add')).click();
  await settle();
  enter('phrase', 'Licht an');
  enter('aliases', 'Licht einschalten\nLeuchte an');
  enter('arg-domain', 'light');
  enter('arg-service', 'turn_on');
  enter('arg-entity_id', 'light.kitchen');
  // A fresh record remains local until the user creates it.
  expect(await autosave.flush()).toBe(true);
  expect(operation).toHaveBeenCalledTimes(1);
  button(t('common.create')).click();
  await settle();
  expect(commands[0]).toEqual({
    phrase: 'Licht an',
    aliases: ['Licht einschalten', 'Leuchte an'],
    tool: 'ha_call_service',
    arguments: {
      domain: 'light',
      service: 'turn_on',
      entity_id: 'light.kitchen',
    },
  });
  button(t('common.edit')).click();
  await settle();
  enter('phrase', 'Küche an');
  let release;
  gate = new Promise((resolve) => {
    release = resolve;
  });
  const firstSave = autosave.runSave('manual');
  enter('aliases', 'Küchenlicht an');
  release();
  expect(await firstSave).toBe(true);
  await settle();
  expect(document.getElementById('aliases').value).toBe('Küchenlicht an');
  expect(await autosave.flush()).toBe(true);
  expect(commands[0].aliases).toEqual(['Küchenlicht an']);
  button(t('common.close')).click();
  await settle();
  invalidate({ change: { resource: 'wakeword_commands' } });
  await settle();
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  button(t('common.remove')).click();
  await settle();
  [...document.querySelectorAll('[role="dialog"] button')]
    .find((node) => node.textContent.trim() === t('common.remove'))
    .click();
  await settle();
  expect(commands).toEqual([]);
});
