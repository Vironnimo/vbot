export function positiveIntegerOrDefault(value, fallback) {
  const numberValue = Number(value);

  return Number.isInteger(numberValue) && numberValue > 0
    ? numberValue
    : fallback;
}

export function textOrEmpty(value) {
  if (value === null || value === undefined) {
    return '';
  }

  return String(value).trim();
}

export function textOrFallback(value, fallback) {
  const normalized = textOrEmpty(value);

  return normalized.length > 0 ? normalized : fallback;
}
