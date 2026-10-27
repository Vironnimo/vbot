// Text rules of the SOUL.md editor. A browser textarea reports every line
// break as "\n", so comparisons and counts use that form of the file.

const encoder = new TextEncoder();

export function normalizeSoulText(text) {
  return String(text ?? '').replace(/\r\n?/g, '\n');
}

// Lines and UTF-8 bytes of the text as it would be saved.
export function soulStats(text) {
  const normalized = normalizeSoulText(text);
  const trimmed = normalized.replace(/\n$/, '');
  return {
    lines: trimmed ? trimmed.split('\n').length : 0,
    bytes: encoder.encode(normalized).length,
  };
}
