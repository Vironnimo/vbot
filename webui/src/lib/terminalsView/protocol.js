// This editor represents the operator API's executable and literal arguments,
// not shell syntax. Keep Windows path separators intact outside quoted values.
export function parseTerminalCommandLine(value) {
  const words = [];
  let word = '';
  let quote = '';
  let started = false;
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    if (quote) {
      if (char === quote) {
        quote = '';
      } else if (
        char === '\\' &&
        (value[index + 1] === quote || value[index + 1] === '\\')
      ) {
        word += value[++index];
      } else {
        word += char;
      }
    } else if (char === '"' || char === "'") {
      quote = char;
      started = true;
    } else if (/\s/.test(char)) {
      if (started) {
        words.push(word);
        word = '';
        started = false;
      }
    } else {
      word += char;
      started = true;
    }
  }
  if (quote) return { error: 'unclosedQuote' };
  if (started) words.push(word);
  if (words.some((part) => !part.trim())) return { error: 'emptyArgument' };
  const [command, ...args] = words;
  return { command, args };
}

export function formatTerminalCommandLine(command, args = []) {
  if (!command) return '';
  return [command, ...args]
    .map((part) =>
      /[\s"']/.test(part)
        ? `"${part.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`
        : part,
    )
    .join(' ');
}

// A WS frame boundary can split an ANSI escape sequence or a UTF-8 code
// point in two. Feeding xterm the torn half corrupts its parser (it can
// swallow the output after a reconnect), so a trailing partial escape is
// held back until the next frame completes it. Everything else passes
// through byte-exact: the server-side renderer consumes the same bytes,
// so any rewriting here would desynchronize the viewer's buffer from the
// authoritative snapshot source.
export function createPtyFrameSanitizer() {
  let pending = '';

  function next(chunk) {
    const combined = pending + chunk;
    if (combined === '') {
      pending = '';
      return '';
    }
    const lastEsc = combined.lastIndexOf('\x1b');
    if (lastEsc !== -1 && PARTIAL_ESC.test(combined.slice(lastEsc))) {
      pending = combined.slice(lastEsc);
      return combined.slice(0, lastEsc);
    }
    pending = '';
    return combined;
  }

  function flush() {
    // The held tail can only ever be an escape start; its rest never
    // arrives at end of stream, so dropping it protects the parser.
    pending = '';
    return '';
  }

  return { next, flush };
}

// eslint-disable-next-line no-control-regex -- intentional ESC byte in ANSI sequence parser
const PARTIAL_ESC = /^\x1b(?:\[\d*)?$/;
