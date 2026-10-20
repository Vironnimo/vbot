// Agent Secrets in the WebUI: the kinds, fields, lifetimes and sinks the
// server's `agent_secrets.*` contract uses, their labels, and the `/secret`
// command text rules the composer follows. Pure and framework-free.
//
// The kind fields, optional fields and default sinks mirror
// `core/secrets/_agent_secret_model.py` (KIND_FIELDS, OPTIONAL_KIND_FIELDS,
// DEFAULT_SINKS); the server validates every value again.
import { t } from './i18n.js';

export const SECRET_KINDS = ['token', 'login', 'text'];
export const SECRET_SINKS = [
  'environment',
  'terminal',
  'files',
  'typing',
  'external_tools',
];

const KIND_FIELDS = {
  token: [{ name: 'value', concealed: true, optional: false }],
  login: [
    { name: 'username', concealed: false, optional: true },
    { name: 'password', concealed: true, optional: false },
    { name: 'url', concealed: false, optional: true },
    { name: 'totp', concealed: true, optional: true },
  ],
  text: [{ name: 'value', concealed: true, optional: false }],
};

const DEFAULT_SINKS = {
  token: ['environment', 'files'],
  login: ['environment', 'terminal', 'typing'],
  text: ['environment', 'terminal', 'files', 'typing'],
};

const KIND_LABELS = {
  token: () => t('secrets.kind.token'),
  login: () => t('secrets.kind.login'),
  text: () => t('secrets.kind.text'),
};

const FIELD_LABELS = {
  value: () => t('secrets.field.value'),
  username: () => t('secrets.field.username'),
  password: () => t('secrets.field.password'),
  url: () => t('secrets.field.url'),
  totp: () => t('secrets.field.totp'),
};

const SINK_LABELS = {
  environment: () => t('secrets.sink.environment'),
  terminal: () => t('secrets.sink.terminal'),
  files: () => t('secrets.sink.files'),
  typing: () => t('secrets.sink.typing'),
  external_tools: () => t('secrets.sink.externalTools'),
};

const LIFETIME_LABELS = {
  permanent: () => t('secrets.lifetime.permanent'),
  session: () => t('secrets.lifetime.session'),
  until: () => t('secrets.lifetime.until'),
  once: () => t('secrets.lifetime.once'),
};

// A Secret limited in time from the composer form or a request answer.
export const SECRET_DURATIONS = [
  { id: '1h', ms: 3_600_000, label: () => t('secrets.duration.1h') },
  { id: '8h', ms: 8 * 3_600_000, label: () => t('secrets.duration.8h') },
  { id: '1d', ms: 86_400_000, label: () => t('secrets.duration.1d') },
  { id: '7d', ms: 7 * 86_400_000, label: () => t('secrets.duration.7d') },
  { id: '30d', ms: 30 * 86_400_000, label: () => t('secrets.duration.30d') },
];

export function kindFields(kind) {
  return KIND_FIELDS[kind] ?? KIND_FIELDS.token;
}

export function defaultSinks(kind) {
  return [...(DEFAULT_SINKS[kind] ?? DEFAULT_SINKS.token)];
}

export function kindLabel(kind) {
  return KIND_LABELS[kind]?.() ?? String(kind ?? '');
}

export function fieldLabel(name) {
  return FIELD_LABELS[name]?.() ?? String(name ?? '');
}

export function sinkLabel(sink) {
  return SINK_LABELS[sink]?.() ?? String(sink ?? '');
}

export function lifetimeLabel(lifetime) {
  return LIFETIME_LABELS[lifetime]?.() ?? String(lifetime ?? '');
}

// `api-key` -> `API_KEY` as the server normalizes a Secret name, or '' when
// the text cannot become one (it must start with a letter).
export function normalizeSecretName(raw) {
  const text = String(raw ?? '')
    .trim()
    .replace(/[\s.\-/:]+/g, '_')
    .toUpperCase()
    .replace(/[^A-Z0-9_]/g, '')
    .replace(/^_+|_+$/g, '')
    .slice(0, 64);
  return /^[A-Z][A-Z0-9_]{0,63}$/.test(text) ? text : '';
}

// A source field name as a Secret field name, or '' when it cannot be one.
export function normalizeFieldName(raw) {
  const text = String(raw ?? '')
    .trim()
    .replace(/[\s.-]+/g, '_')
    .toLowerCase();
  return /^[a-z][a-z0-9_]{0,31}$/.test(text) ? text : '';
}

export function secretReference(name, field = '') {
  return field ? `{{secret:${name}.${field}}}` : `{{secret:${name}}}`;
}

// The instant `durationId` from now, as the RFC 3339 `expires_at` the RPCs take.
export function expiryAfter(durationId, nowMs = Date.now()) {
  const duration = SECRET_DURATIONS.find((item) => item.id === durationId);
  return duration ? new Date(nowMs + duration.ms).toISOString() : '';
}

// The lifetimes a user picks when giving a Session's Agent a Secret (the
// composer form, a request answer): this Session, one use, permanently, or a
// time from now. A choice is `session`, `once`, `permanent` or `for:<id>`.
export function lifetimeChoices() {
  return [
    { value: 'session', label: t('secrets.lifetime.session') },
    { value: 'once', label: t('secrets.lifetime.once') },
    { value: 'permanent', label: t('secrets.lifetime.permanent') },
    ...SECRET_DURATIONS.map((duration) => ({
      value: `for:${duration.id}`,
      label: duration.label(),
    })),
  ];
}

// A lifetime choice as RPC parameters: `{ lifetime, expires_at? }`.
export function lifetimeChoiceParams(choice, nowMs = Date.now()) {
  const expiresAt = expiryAfter(lifetimeChoiceDuration(choice), nowMs);
  if (expiresAt) {
    return { lifetime: 'until', expires_at: expiresAt };
  }
  return {
    lifetime: ['once', 'permanent'].includes(choice) ? choice : 'session',
  };
}

// The duration id of a `for:<id>` choice (what `/secret --for` takes), or ''.
export function lifetimeChoiceDuration(choice) {
  return typeof choice === 'string' && choice.startsWith('for:')
    ? choice.slice(4)
    : '';
}

// The overview `agent_secrets.list` returns, with every list present.
export function normalizeSecretOverview(result) {
  const list = (value) => (Array.isArray(value) ? value : []);
  return {
    secrets: list(result?.secrets),
    sources: list(result?.sources),
    requests: list(result?.requests),
  };
}

// The `/secret` command's argument can hold a Secret value, so the composer
// keeps no copy of such text (drafts, send history). A bare `/secret` holds
// none; the composer opens the Secret form for it instead of sending it.
const BARE_SECRET_COMMAND = /^\s*\/secret\s*$/i;
const SECRET_COMMAND_WITH_ARGUMENT = /^\s*\/secret\s+\S/i;

export function isBareSecretCommand(text) {
  return typeof text === 'string' && BARE_SECRET_COMMAND.test(text);
}

export function holdsSecretArgument(text) {
  return typeof text === 'string' && SECRET_COMMAND_WITH_ARGUMENT.test(text);
}

// The `/secret` command that stores what the composer form holds, for a
// draft that has no Session yet: the command creates it
// (`core/secrets/commands.py`). Options take one word each and the
// description takes quotes, so a username with a space or a description with
// a quote cannot be written; `{ problem }` names the field then.
export function secretCommandText({
  name,
  kind,
  values,
  lifetime,
  duration = '',
  description = '',
}) {
  const parts = ['/secret', name];
  if (lifetime === 'permanent') parts.push('--permanent');
  else if (lifetime === 'once') parts.push('--once');
  else if (lifetime === 'until') parts.push('--for', duration);
  const value = kind === 'login' ? values.password : values.value;
  if (kind === 'login') {
    parts.push('--kind', 'login');
    const username = values.username ?? '';
    if (/\s/.test(username)) return { problem: 'username' };
    if (username) parts.push('--user', username);
  } else if (kind === 'text') {
    parts.push('--kind', 'text');
  }
  if (description) {
    if (description.includes('"')) return { problem: 'description' };
    parts.push('--description', `"${description}"`);
  }
  // `--` ends the options, so a value may start with `--`.
  parts.push('--', value);
  return { text: parts.join(' ') };
}
