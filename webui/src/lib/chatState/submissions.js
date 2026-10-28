import {
  ApiClientError,
  RPC_ERROR_HTTP,
  RPC_ERROR_NETWORK,
  RPC_ERROR_RESPONSE,
} from '../api/transport.js';
import { holdsSecretArgument, isBareSecretCommand } from '../agentSecrets.js';

const STORAGE_KEY = 'vbot.pending-chat-inputs';
const MAX_PENDING_INPUTS = 32;

// The controller owns submissions. This private helper keeps an ordinary
// message's exact prepared request and identity across uncertain acknowledgments.
// Restoring a page only reads these records; a deliberate send performs retries.
export function createChatSubmissions(send, storage = browserStorage()) {
  let pending = [];

  function persist(entries) {
    try {
      if (entries.length)
        storage?.setItem(STORAGE_KEY, JSON.stringify(entries));
      else storage?.removeItem(STORAGE_KEY);
    } catch {
      // A full or unavailable storage keeps the identities for this controller.
    }
  }

  function forget(record) {
    pending = pending.filter((entry) => entry.inputId !== record.inputId);
    persist(
      readPending(storage).filter((entry) => entry.inputId !== record.inputId),
    );
  }

  return async function submit(scope, params) {
    // Slash input can execute Commands, including a Secret command. Retain its
    // identity only in memory for an explicit retry: ordinary slash text is
    // deduplicated by admission, while Commands ignore the id as before.
    // Neither automatic replay nor browser persistence is safe for Commands.
    const commandCandidate = isSlashCommandCandidate(params.content);
    const request = JSON.stringify(params);
    let record = [
      ...pending,
      ...(commandCandidate ? [] : readPending(storage)),
    ].find((entry) => entry.scope === scope && entry.request === request);
    if (!record) {
      record = { scope, request, inputId: newInputId() };
      pending = [...pending.slice(-(MAX_PENDING_INPUTS - 1)), record];
      if (!commandCandidate) {
        persist([...readPending(storage), record].slice(-MAX_PENDING_INPUTS));
      }
    } else if (!pending.some((entry) => entry.inputId === record.inputId)) {
      pending = [...pending.slice(-(MAX_PENDING_INPUTS - 1)), record];
    }
    const prepared = {
      ...JSON.parse(record.request),
      input_id: record.inputId,
    };
    for (let attempt = 0; ; attempt += 1) {
      try {
        const response = await send(prepared);
        if (commandCandidate && response?.command_handled === true) {
          forget(record);
          return response;
        }
        if (
          !response ||
          (response.accepted !== true &&
            response.queued !== true &&
            typeof response.run_id !== 'string')
        ) {
          throw new ApiClientError(
            RPC_ERROR_RESPONSE,
            'Chat response names no accepted input, Run or Queue item',
          );
        }
        forget(record);
        return response;
      } catch (error) {
        const uncertain = uncertainAcknowledgment(error);
        if (!uncertain) forget(record);
        if (uncertain && attempt === 0 && !commandCandidate) continue;
        throw error;
      }
    }
  };
}

function uncertainAcknowledgment(error) {
  return (
    error?.code === RPC_ERROR_NETWORK ||
    error?.code === RPC_ERROR_RESPONSE ||
    error?.code === 'internal_error' ||
    (error?.code === RPC_ERROR_HTTP && error.status >= 500)
  );
}

export function isSlashCommandCandidate(content) {
  const texts =
    typeof content === 'string'
      ? [content]
      : Array.isArray(content)
        ? content
            .filter((block) => block?.type === 'text')
            .map((block) => block.text)
        : [];
  if (texts.length !== 1) return false;
  const text = texts[0];
  // CommandDispatcher accepts mixed content only for /secret. An ordinary
  // slash-prefixed message with attachments needs the same retry identity as
  // other user input; a Secret value must never enter submission storage.
  if (
    Array.isArray(content) &&
    content.length !== 1 &&
    !isBareSecretCommand(text) &&
    !holdsSecretArgument(text)
  )
    return false;
  return typeof text === 'string' && text.trimStart().startsWith('/');
}

function newInputId() {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return `inp_${globalThis.crypto.randomUUID()}`;
  }
  return `inp_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}_${Math.random().toString(36).slice(2)}`;
}

function browserStorage() {
  try {
    return globalThis.sessionStorage ?? null;
  } catch {
    return null;
  }
}

function readPending(storage) {
  try {
    const entries = JSON.parse(storage?.getItem(STORAGE_KEY) ?? '[]');
    return Array.isArray(entries)
      ? entries
          .filter(
            (entry) =>
              typeof entry?.scope === 'string' &&
              typeof entry.request === 'string' &&
              typeof entry.inputId === 'string' &&
              entry.inputId.length > 0 &&
              entry.inputId.length <= 128,
          )
          .slice(-MAX_PENDING_INPUTS)
      : [];
  } catch {
    return [];
  }
}
