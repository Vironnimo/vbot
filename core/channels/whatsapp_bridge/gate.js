// Account identity comes from Baileys, never from a caller-supplied destination.
export function normalizeJid(value) {
  return typeof value === 'string' ? value.replace(/:\d+@/, '@') : '';
}

export function acceptsSelfMessage(message, user, sent, since) {
  const key = message.key || {};
  const identities = new Set([normalizeJid(user?.id), normalizeJid(user?.lid)].filter(Boolean));
  return key.fromMe === true && Boolean(key.id) &&
    identities.has(normalizeJid(key.remoteJid)) && !sent.has(key.id) &&
    Number(message.messageTimestamp || 0) >= since;
}
