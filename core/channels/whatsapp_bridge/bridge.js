import { makeWASocket, useMultiFileAuthState, DisconnectReason, downloadMediaMessage, normalizeMessageContent, generateMessageIDV2 } from '@whiskeysockets/baileys';
import pino from 'pino';
import QRCode from 'qrcode';
import { createInterface } from 'node:readline';
import { mkdir, readFile, writeFile, rename } from 'node:fs/promises';
import { join } from 'node:path';
import { acceptsSelfMessage } from './gate.js';

const directory = process.argv[2];
const maxBytes = Number(process.argv[3]);
if (!directory || !Number.isSafeInteger(maxBytes) || maxBytes <= 0) process.exit(2);
await mkdir(directory, { recursive: true, mode: 0o700 });
const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
// stdout is a private framed protocol. Library logs and credentials never leave it.
const logger = pino({ level: 'silent' });
const stateFile = join(directory, 'delivery.json');
let delivery;
try { delivery = JSON.parse(await readFile(stateFile, 'utf8')); }
catch (error) { if (error.code !== 'ENOENT') throw new Error('Cannot read delivery state'); }
delivery ||= { since: Math.floor(Date.now() / 1000), sent: [] };
if (!Number.isFinite(delivery.since) || !Array.isArray(delivery.sent)) throw new Error('Invalid delivery state');
const sent = new Set(delivery.sent);
async function saveDelivery() {
  delivery.sent = [...sent].slice(-4096);
  await writeFile(stateFile + '.tmp', JSON.stringify(delivery), { mode: 0o600 });
  await rename(stateFile + '.tmp', stateFile);
}
await saveDelivery();
const { state, saveCreds } = await useMultiFileAuthState(join(directory, 'auth'));
const socket = makeWASocket({ auth: state, logger, syncFullHistory: false, shouldSyncHistoryMessage: () => false, markOnlineOnConnect: false });
const messages = new Map();
let connected = false;
let authWrites = Promise.resolve();
socket.ev.on('creds.update', () => { authWrites = authWrites.then(saveCreds).catch(() => { emit({ event: 'failed', reason: 'credential_write_failed' }); process.exit(1); }); });
socket.ev.on('connection.update', async update => {
  if (update.qr) emit({ event: 'qr', image: await QRCode.toDataURL(update.qr, { width: 320, margin: 2 }) });
  if (update.connection === 'open') { connected = true; emit({ event: 'connected' }); }
  if (update.connection === 'close') {
    connected = false;
    const loggedOut = update.lastDisconnect?.error?.output?.statusCode === DisconnectReason.loggedOut;
    emit({ event: 'closed', reason: loggedOut ? 'logged_out' : 'disconnected' });
    await authWrites;
    process.exit(loggedOut ? 2 : 1);
  }
});

socket.ev.on('messages.upsert', ({ messages: incoming, type }) => {
  if (!connected || !['notify', 'append'].includes(type)) return;
  for (const raw of incoming) {
    if (!acceptsSelfMessage(raw, socket.user, sent, delivery.since)) continue;
    const body = normalizeMessageContent(raw.message);
    if (!body) continue;
    const text = body.conversation || body.extendedTextMessage?.text || '';
    const media = body.imageMessage || body.audioMessage || body.videoMessage || body.documentMessage;
    if (!text && !media) continue;
    messages.set(raw.key.id, raw);
    while (messages.size > 256) messages.delete(messages.keys().next().value);
    emit({ event: 'message', id: raw.key.id, text: text || media?.caption || '', files: media ? [{ id: raw.key.id, name: media.fileName || 'whatsapp-media', size: Number(media.fileLength || 0), mimetype: media.mimetype }] : [] });
  }
});

async function command(request) {
  if (!connected) throw new Error('not_connected');
  if (request.action === 'download') {
    const raw = messages.get(request.message_id);
    if (!raw) throw new Error('media_expired');
    const stream = await downloadMediaMessage(raw, 'stream', {}, { logger, reuploadRequest: socket.updateMediaMessage });
    const chunks = [];
    let size = 0;
    for await (const chunk of stream) {
      size += chunk.length;
      if (size > maxBytes) { stream.destroy(); throw new Error('media_too_large'); }
      chunks.push(chunk);
    }
    return { data: Buffer.concat(chunks).toString('base64') };
  }
  if (request.action !== 'send' || request.target !== 'self') throw new Error('invalid_target');
  let content;
  if (request.file) {
    const bytes = Buffer.from(request.file.data, 'base64');
    if (bytes.length > maxBytes) throw new Error('media_too_large');
    const mime = request.file.mimetype;
    if (mime.startsWith('image/')) content = { image: bytes, mimetype: mime };
    else if (mime.startsWith('audio/')) content = { audio: bytes, mimetype: mime };
    else if (mime.startsWith('video/')) content = { video: bytes, mimetype: mime };
    else content = { document: bytes, mimetype: mime, fileName: request.file.name };
  } else {
    if (typeof request.text !== 'string' || !request.text.trim()) throw new Error('empty_message');
    content = { text: request.text };
  }
  const id = generateMessageIDV2(socket.user.id);
  // Persist before transmission: an immediate echo or restart cannot become a new Run.
  sent.add(id);
  while (sent.size > 4096) sent.delete(sent.values().next().value);
  await saveDelivery();
  await socket.sendMessage(socket.user.id, content, { messageId: id });
  return {};
}

const input = createInterface({ input: process.stdin });
let commands = Promise.resolve();
input.on('line', line => {
  commands = commands.then(async () => {
    let request;
    try {
      if (line.length > maxBytes * 1.4 + 8192) throw new Error('frame_too_large');
      request = JSON.parse(line);
      const result = await command(request);
      emit({ id: request.id, ok: true, ...result });
    } catch {
      emit({ id: request?.id, ok: false, error: 'operation_failed' });
    }
  });
});
input.on('close', () => { socket.end(undefined); process.exit(0); });
