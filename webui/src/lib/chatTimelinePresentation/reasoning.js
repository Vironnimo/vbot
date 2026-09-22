// A heading is optional display text, never an execution status or a Tool result.
export function reasoningSummaryTitle(source) {
  if (typeof source !== 'string') return '';
  const firstLine = source.trimStart().split('\n', 1)[0].trim();
  const match =
    firstLine.match(/^\*\*([^*]+)\*\*$/) ??
    firstLine.match(/^#{1,6}\s+(.+?)(?:\s+#+)?$/);
  return match && match[1].length <= 180 ? match[1].trim() : '';
}
