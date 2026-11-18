// Pages supply labels visible to the user; the host supplies the location.
export function validPageChatContext(value) {
  const label = (s) =>
    typeof s === 'string' &&
    s.trim().length > 0 &&
    s.length <= 256 &&
    !/[\p{C}]/u.test(s);
  return (
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    Object.entries(value).every(([key, item]) =>
      key === 'options'
        ? Array.isArray(item) && item.length <= 32 && item.every(label)
        : key === 'docs'
          ? label(item) && /^references\/[a-zA-Z0-9_-]+\.md$/.test(item)
          : ['section', 'field', 'selection'].includes(key) && label(item),
    ) &&
    new TextEncoder().encode(JSON.stringify(value)).length <= 6000
  );
}

export const sessionPresentations = ['conversation', 'compact', 'full'];
