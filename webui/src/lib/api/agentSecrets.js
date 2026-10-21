import { rpc, requireNonEmptyString, requirePlainObject } from './transport.js';

// Agent Secrets: values an Agent uses through `{{secret:NAME}}` references
// without seeing them (Settings -> Secrets, the Chat request card, the
// composer's `/secret` form). Apart from vBot's own credentials
// (`secrets.*`). A concealed value reaches the client only through
// `revealAgentSecret`; every other read carries visible fields only.

// `{ secrets, sources, requests, kinds, lifetimes, sinks }`.
export function listAgentSecrets(options = {}) {
  return rpc('agent_secrets.list', {}, options);
}

export function createAgentSecret(params, options = {}) {
  requirePlainObject(
    params,
    'Secret parameters must be an object',
    'agent_secrets.create',
  );
  return rpc('agent_secrets.create', params, options);
}

// `params.id` names the Secret; the other keys change only what they name.
export function updateAgentSecret(params, options = {}) {
  requirePlainObject(
    params,
    'Secret parameters must be an object',
    'agent_secrets.update',
  );
  requireNonEmptyString(
    params.id,
    'Secret id must be a non-empty string',
    'agent_secrets.update',
  );
  return rpc('agent_secrets.update', params, options);
}

export function deleteAgentSecret(id, options = {}) {
  requireNonEmptyString(
    id,
    'Secret id must be a non-empty string',
    'agent_secrets.delete',
  );
  return rpc('agent_secrets.delete', { id }, options);
}

// The stored value of one field, for the user's explicit reveal only.
export function revealAgentSecret(id, field, options = {}) {
  requireNonEmptyString(
    id,
    'Secret id must be a non-empty string',
    'agent_secrets.reveal',
  );
  requireNonEmptyString(
    field,
    'Secret field must be a non-empty string',
    'agent_secrets.reveal',
  );
  return rpc('agent_secrets.reveal', { id, field }, options);
}

// The user gives the Agent of one Session a Secret (`/secret` and its form):
// a pending request of that name is answered, else the Secret is created or
// its value replaced. Resolves `{ secret, replaced }`.
export function giveAgentSecret(params, options = {}) {
  requirePlainObject(
    params,
    'Secret parameters must be an object',
    'agent_secrets.give',
  );
  return rpc('agent_secrets.give', params, options);
}

// Answer an Agent's pending request: `{ id, values, lifetime?, expires_at? }`.
export function answerSecretRequest(params, options = {}) {
  requirePlainObject(
    params,
    'Request answer must be an object',
    'agent_secrets.answer',
  );
  requireNonEmptyString(
    params.id,
    'Request id must be a non-empty string',
    'agent_secrets.answer',
  );
  return rpc('agent_secrets.answer', params, options);
}

export function declineSecretRequest(id, options = {}) {
  requireNonEmptyString(
    id,
    'Request id must be a non-empty string',
    'agent_secrets.decline',
  );
  return rpc('agent_secrets.decline', { id }, options);
}

// `inputs` holds the Source's `unlock_fields` by name.
export function unlockSecretSource(source, inputs, options = {}) {
  requireNonEmptyString(
    source,
    'Source must be a non-empty string',
    'agent_secrets.source_unlock',
  );
  return rpc(
    'agent_secrets.source_unlock',
    { source, inputs: inputs ?? {} },
    options,
  );
}

export function lockSecretSource(source, options = {}) {
  requireNonEmptyString(
    source,
    'Source must be a non-empty string',
    'agent_secrets.source_lock',
  );
  return rpc('agent_secrets.source_lock', { source }, options);
}

// The entries of an unlocked Source, optionally of one container.
export function listSecretSourceEntries(source, container = '', options = {}) {
  requireNonEmptyString(
    source,
    'Source must be a non-empty string',
    'agent_secrets.source_entries',
  );
  return rpc(
    'agent_secrets.source_entries',
    container ? { source, container } : { source },
    options,
  );
}
