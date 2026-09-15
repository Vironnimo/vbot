import { test } from 'node:test';
import assert from 'node:assert/strict';
import { acceptsSelfMessage } from '../../../core/channels/whatsapp_bridge/gate.js';

const user = { id: '491234:8@s.whatsapp.net', lid: '987:8@lid' };
const message = { key: { id: 'incoming', fromMe: true, remoteJid: '491234@s.whatsapp.net' }, messageTimestamp: 200 };
test('accepts phone and LID self-chat identities', () => {
  assert.equal(acceptsSelfMessage(message, user, new Set(), 100), true);
  assert.equal(acceptsSelfMessage({ ...message, key: { ...message.key, remoteJid: '987@lid' } }, user, new Set(), 100), true);
});
test('rejects contacts, groups, other senders and old history', () => {
  for (const remoteJid of ['491235@s.whatsapp.net', 'group@g.us', 'status@broadcast', '']) {
    assert.equal(acceptsSelfMessage({ ...message, key: { ...message.key, remoteJid } }, user, new Set(), 100), false);
  }
  assert.equal(acceptsSelfMessage({ ...message, key: { ...message.key, fromMe: false } }, user, new Set(), 100), false);
  assert.equal(acceptsSelfMessage(message, user, new Set(), 300), false);
});
test('rejects sent IDs including after a persisted-state reload', () => {
  const persisted = JSON.stringify(['incoming']);
  assert.equal(acceptsSelfMessage(message, user, new Set(JSON.parse(persisted)), 100), false);
});
