import common from './i18n/common.js';
import chat from './i18n/chat.js';
import configuration from './i18n/configuration.js';
import management from './i18n/management.js';
import operations from './i18n/operations.js';
import insights from './i18n/insights.js';

const DEFAULT_LOCALE = 'en';

// Keep keys globally unique even though the English catalog is authored by domain.
const mergedEnglish = {};
for (const catalog of [
  common,
  chat,
  configuration,
  management,
  operations,
  insights,
]) {
  for (const [key, value] of Object.entries(catalog)) {
    if (Object.hasOwn(mergedEnglish, key))
      throw new Error(`Duplicate i18n key: ${key}`);
    mergedEnglish[key] = value;
  }
}
export const englishCatalog = Object.freeze(mergedEnglish);

// The core catalog plus the catalogs Extension pages register at startup.
const englishText = { ...englishCatalog };

const catalogs = Object.freeze({
  [DEFAULT_LOCALE]: englishText,
});

let activeLocale = DEFAULT_LOCALE;

function hasText(value) {
  return typeof value === 'string' && value.length > 0;
}

function interpolate(template, values) {
  if (!values) {
    return template;
  }

  return template.replace(/\{([A-Za-z0-9_]+)\}/g, (match, name) => {
    if (!Object.prototype.hasOwnProperty.call(values, name)) {
      return match;
    }

    return String(values[name]);
  });
}

export function t(key, fallback, values) {
  const catalog = catalogs[activeLocale] ?? catalogs[DEFAULT_LOCALE];
  const translation = catalog[key] ?? catalogs[DEFAULT_LOCALE][key];
  const template = hasText(translation)
    ? translation
    : hasText(fallback)
      ? fallback
      : key;

  return interpolate(template, values);
}

// Resolves a composed key built from a server-sent code. A code without a
// catalog entry renders `fallback` (usually the code itself) instead of the key.
export function tOr(key, fallback, values) {
  const catalog = catalogs[activeLocale] ?? catalogs[DEFAULT_LOCALE];
  const translation = catalog[key] ?? catalogs[DEFAULT_LOCALE][key];

  return interpolate(hasText(translation) ? translation : fallback, values);
}

// Adds an Extension page's own English text. Keys the core catalog or an
// earlier registration already defines are rejected, so a page cannot replace
// existing text.
export function registerCatalog(entries) {
  const duplicates = Object.keys(entries).filter((key) =>
    Object.hasOwn(englishText, key),
  );
  if (duplicates.length > 0) {
    throw new Error(`Duplicate i18n keys: ${duplicates.join(', ')}`);
  }
  Object.assign(englishText, entries);
}

export function init(locale = DEFAULT_LOCALE) {
  activeLocale = catalogs[locale] ? locale : DEFAULT_LOCALE;

  return activeLocale;
}

// BCP 47 tag of the active UI language, for Intl formatters. Dates and times
// must follow the app language, not the browser/OS locale — a German OS must
// not inject German month names or comma decimals into the English UI.
export function activeLocaleTag() {
  return activeLocale;
}
