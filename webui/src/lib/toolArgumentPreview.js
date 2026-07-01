// Incremental extraction of completed top-level string fields from a tool
// call's partially streamed JSON arguments. Argument fragments reach the
// browser long before the call dispatches, so scanning them as they arrive
// lets the UI label a "preparing" tool row (e.g. a write's file path) without
// waiting for the full arguments — which for file writes means the whole file
// content — to finish streaming.

export const TOOL_ARGUMENT_PREVIEW_VALUE_MAX_LENGTH = 200;

// Escape sequences make the raw token longer than its decoded value, so the
// raw capture buffer gets headroom before a value counts as truncated.
const RAW_TOKEN_MAX_LENGTH = TOOL_ARGUMENT_PREVIEW_VALUE_MAX_LENGTH * 2;
const TRUNCATION_SUFFIX = '…';

const PHASE_BEFORE_OBJECT = 0;
const PHASE_SCANNING = 1;
// Terminal: the top-level object closed, or the fragment cannot be an
// arguments object at all. Either way all further input is ignored.
const PHASE_FINISHED = 2;

// Position within the top-level object, tracked only at nesting depth 1.
const SLOT_KEY = 0;
const SLOT_COLON = 1;
const SLOT_VALUE = 2;
const SLOT_AFTER_VALUE = 3;

const ROLE_KEY = 0;
const ROLE_CAPTURE = 1;
const ROLE_SKIP = 2;

const JSON_WHITESPACE = new Set([' ', '\t', '\n', '\r']);

export function createToolArgumentPreviewScanner() {
  let phase = PHASE_BEFORE_OBJECT;
  let depth = 0;
  let slot = SLOT_KEY;
  let inString = false;
  let escaped = false;
  let stringRole = ROLE_SKIP;
  let token = '';
  let tokenTruncated = false;
  let currentKey = null;
  let fields = null;

  function push(text) {
    if (typeof text !== 'string' || !text) {
      return false;
    }
    let changed = false;
    for (let index = 0; index < text.length; index += 1) {
      if (phase === PHASE_FINISHED) {
        break;
      }
      if (consumeCharacter(text[index])) {
        changed = true;
      }
    }
    return changed;
  }

  function fieldsSnapshot() {
    return fields ? { ...fields } : null;
  }

  function consumeCharacter(character) {
    if (phase === PHASE_BEFORE_OBJECT) {
      if (JSON_WHITESPACE.has(character)) {
        return false;
      }
      if (character === '{') {
        phase = PHASE_SCANNING;
        depth = 1;
        slot = SLOT_KEY;
        return false;
      }
      phase = PHASE_FINISHED;
      return false;
    }
    if (inString) {
      return consumeStringCharacter(character);
    }
    if (JSON_WHITESPACE.has(character)) {
      return false;
    }
    switch (character) {
      case '"':
        inString = true;
        escaped = false;
        token = '';
        tokenTruncated = false;
        if (depth === 1 && slot === SLOT_KEY) {
          stringRole = ROLE_KEY;
        } else if (depth === 1 && slot === SLOT_VALUE) {
          stringRole = ROLE_CAPTURE;
        } else {
          stringRole = ROLE_SKIP;
        }
        return false;
      case '{':
      case '[':
        if (depth === 1 && slot === SLOT_VALUE) {
          slot = SLOT_AFTER_VALUE;
        }
        depth += 1;
        return false;
      case '}':
      case ']':
        depth -= 1;
        if (depth <= 0) {
          phase = PHASE_FINISHED;
        }
        return false;
      case ':':
        if (depth === 1 && slot === SLOT_COLON) {
          slot = SLOT_VALUE;
        }
        return false;
      case ',':
        if (depth === 1) {
          slot = SLOT_KEY;
        }
        return false;
      default:
        // Primitive value characters (numbers, true/false/null). Only string
        // values are captured; just note the value slot is occupied.
        if (depth === 1 && slot === SLOT_VALUE) {
          slot = SLOT_AFTER_VALUE;
        }
        return false;
    }
  }

  function consumeStringCharacter(character) {
    if (escaped) {
      escaped = false;
      appendTokenCharacter(character);
      return false;
    }
    if (character === '\\') {
      escaped = true;
      appendTokenCharacter(character);
      return false;
    }
    if (character === '"') {
      inString = false;
      return endStringToken();
    }
    appendTokenCharacter(character);
    return false;
  }

  function appendTokenCharacter(character) {
    if (stringRole === ROLE_SKIP) {
      return;
    }
    if (token.length >= RAW_TOKEN_MAX_LENGTH) {
      tokenTruncated = true;
      return;
    }
    token += character;
  }

  function endStringToken() {
    if (stringRole === ROLE_KEY) {
      currentKey = tokenTruncated ? null : decodeStringToken(token);
      slot = SLOT_COLON;
      return false;
    }
    if (stringRole !== ROLE_CAPTURE) {
      return false;
    }
    slot = SLOT_AFTER_VALUE;
    const key = currentKey;
    currentKey = null;
    if (key === null) {
      return false;
    }
    const value = decodePreviewValue(token, tokenTruncated);
    if (value === null || fields?.[key] === value) {
      return false;
    }
    fields = fields ?? {};
    fields[key] = value;
    return true;
  }

  return { push, fields: fieldsSnapshot };
}

function decodeStringToken(rawToken) {
  try {
    const decoded = JSON.parse(`"${rawToken}"`);
    return typeof decoded === 'string' ? decoded : null;
  } catch {
    return null;
  }
}

function decodePreviewValue(rawToken, truncated) {
  const decoded = decodeStringToken(
    truncated ? trimTruncatedEscape(rawToken) : rawToken,
  );
  if (decoded === null) {
    return null;
  }
  if (truncated || decoded.length > TOOL_ARGUMENT_PREVIEW_VALUE_MAX_LENGTH) {
    return (
      decoded.slice(0, TOOL_ARGUMENT_PREVIEW_VALUE_MAX_LENGTH) +
      TRUNCATION_SUFFIX
    );
  }
  return decoded;
}

// A capture buffer cut off at the raw cap may end inside an escape sequence;
// drop the partial escape so the remaining prefix decodes cleanly.
function trimTruncatedEscape(rawToken) {
  let text = rawToken.replace(/\\u[0-9a-fA-F]{0,3}$/, '');
  const trailingBackslashes = text.match(/\\*$/)[0].length;
  if (trailingBackslashes % 2 === 1) {
    text = text.slice(0, -1);
  }
  return text;
}
