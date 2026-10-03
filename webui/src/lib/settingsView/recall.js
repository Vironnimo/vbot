// Conversation search (Recall) settings: the search method, the embedding
// Model picker's options and the chosen Model's description, and the
// semantic index status line.

import { activeLocaleTag, t, tOr } from '../i18n.js';
import { formatRelativeTime } from '../timeText.js';
import {
  describeLocalModelDownload,
  formatDownloadSize,
} from './localModels.js';
import { textOrEmpty } from './values.js';

const RECALL_BACKEND_KEYWORD = 'sqlite_fts';
const RECALL_BACKEND_HYBRID = 'hybrid';

// The first-party backends that rank by meaning and therefore embed
// conversation text.
const MEANING_BACKENDS = Object.freeze(['vector', RECALL_BACKEND_HYBRID]);

// First-party backends in the order the method picker lists them; an
// Extension's backends follow.
const RECALL_BACKEND_DEFAULTS = Object.freeze([
  RECALL_BACKEND_KEYWORD,
  RECALL_BACKEND_HYBRID,
  'vector',
]);

// Index states without a status line: semantic search is off, or no
// embedding Model is chosen yet.
const STATUS_LINE_HIDDEN_STATES = new Set(['disabled', 'unconfigured']);

function normalizeRecallSettings(rawSettings) {
  const recall = rawSettings?.recall ?? {};
  const availableBackends = normalizeRecallBackends(recall.available_backends);
  const backend =
    typeof recall.backend === 'string' &&
    availableBackends.includes(recall.backend)
      ? recall.backend
      : RECALL_BACKEND_KEYWORD;

  return {
    backend,
    available_backends: availableBackends,
  };
}

function normalizeRecallBackends(backends) {
  const values = Array.isArray(backends) ? backends : RECALL_BACKEND_DEFAULTS;
  const normalized = values
    .map((backend) => textOrEmpty(backend))
    .filter((backend) => backend.length > 0);

  return normalized.length > 0
    ? Array.from(new Set(normalized))
    : [...RECALL_BACKEND_DEFAULTS];
}

export function getRecallSettings(settings) {
  return normalizeRecallSettings(settings);
}

export function buildRecallSettingsPayload(formValues) {
  return {
    recall: {
      backend: normalizeRecallSettings({ recall: formValues }).backend,
    },
  };
}

function backendOrder(backend) {
  const index = RECALL_BACKEND_DEFAULTS.indexOf(backend);
  return index === -1 ? RECALL_BACKEND_DEFAULTS.length : index;
}

// The search method picker: keywords, keywords and meaning (recommended),
// meaning only, then any Extension's backends under their id.
export function buildRecallMethodOptions(recallSettings) {
  return normalizeRecallBackends(recallSettings?.available_backends)
    .map((backend, index) => ({ backend, index }))
    .sort(
      (left, right) =>
        backendOrder(left.backend) - backendOrder(right.backend) ||
        left.index - right.index,
    )
    .map(({ backend }) => ({
      value: backend,
      label: tOr(`settings.recall.backends.${backend}`, backend),
      secondaryLabel:
        backend === RECALL_BACKEND_HYBRID
          ? t('settings.recall.methodRecommended')
          : '',
    }));
}

// What the chosen search method finds, for the line under its label.
export function describeRecallMethod(backend) {
  return tOr(
    `settings.recall.backendDescriptions.${backend}`,
    t('settings.recall.methodExtension'),
  );
}

// Whether the backend ranks by meaning, so it needs an embedding Model.
export function recallSearchesByMeaning(backend) {
  return MEANING_BACKENDS.includes(backend);
}

// A USD price or cost: four decimals below $1 so per-million embedding
// prices stay readable, `<$0.0001` for less, a dash when unknown.
function formatCost(value, locale) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    return '—';
  }
  const tiny = value > 0 && value < 0.0001;
  return (
    (tiny ? '<' : '') +
    new Intl.NumberFormat(locale, {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 2,
      maximumFractionDigits: value < 1 && value > 0 ? 4 : 2,
    }).format(tiny ? 0.0001 : value)
  );
}

function finiteOrNull(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

// A target runs on this computer when vBot runs it itself or its Provider is
// a local runtime.
function targetIsLocal(target) {
  return target.kind === 'local' || target.facts?.local === true;
}

// A Model vBot runs itself that is not installed yet; choosing it offers the
// installation.
export function embeddingTargetNeedsInstall(target) {
  return target?.kind === 'local' && target.usable === false;
}

function compareRecommended(left, right) {
  const leftRank = finiteOrNull(left.facts?.recommended_rank) ?? Infinity;
  const rightRank = finiteOrNull(right.facts?.recommended_rank) ?? Infinity;
  if (leftRank !== rightRank) {
    return leftRank - rightRank;
  }
  return left.label.localeCompare(right.label);
}

function priceText(target) {
  const price = finiteOrNull(target.facts?.input_price_per_million);
  if (targetIsLocal(target) && (price === null || price === 0)) {
    return t('settings.recall.model.free');
  }
  return price === null
    ? ''
    : t('settings.recall.model.price', {
        price: formatCost(price, activeLocaleTag()),
      });
}

// The short facts beside a picker option: installation state or price, and
// a language limit.
function optionFacts(target) {
  const facts = [];
  if (embeddingTargetNeedsInstall(target)) {
    const size = formatDownloadSize(target.metadata?.download_bytes);
    facts.push(
      size
        ? t('settings.recall.model.notInstalledSize', { size })
        : t('settings.recall.model.notInstalled'),
    );
  } else if (!target.usable) {
    facts.push(t('settings.recall.model.unavailable'));
  } else {
    facts.push(priceText(target));
  }
  if (target.facts?.multilingual === false) {
    facts.push(t('settings.recall.model.englishOnly'));
  }
  return facts.filter(Boolean).join(' · ');
}

/**
 * The embedding Model picker's options: Models on this computer, then cloud
 * Models, each recommended first. A Model vBot runs itself stays selectable
 * before it is installed, because choosing it offers the installation; any
 * other unusable target is disabled. A saved target no longer offered stays
 * listed so the picker can show it.
 */
export function buildEmbeddingModelOptions(targets, selectedId = '') {
  const list = (Array.isArray(targets) ? targets : [])
    .slice()
    .sort(compareRecommended);
  const groups = [
    [t('settings.recall.model.groupLocal'), list.filter(targetIsLocal)],
    [
      t('settings.recall.model.groupCloud'),
      list.filter((target) => !targetIsLocal(target)),
    ],
  ];
  const options = groups.flatMap(([group, members]) =>
    members.map((target) => ({
      value: target.id,
      label: target.label,
      secondaryLabel: optionFacts(target),
      group,
      disabled: !target.usable && !embeddingTargetNeedsInstall(target),
      searchText: `${target.label} ${target.id}`,
      tooltip: textOrEmpty(target.facts?.note),
    })),
  );
  if (selectedId && !list.some((target) => target.id === selectedId)) {
    options.push({
      value: selectedId,
      label: selectedId,
      secondaryLabel: t('settings.recall.model.unavailable'),
      group: t('settings.recall.model.groupSaved'),
      code: true,
    });
  }
  return options;
}

// The best recommended Model that can be chosen, for the prompt shown while
// no Model is chosen; null when none is recommended.
export function recommendedEmbeddingTarget(targets) {
  const list = Array.isArray(targets) ? targets : [];
  return (
    list
      .filter(
        (target) =>
          finiteOrNull(target.facts?.recommended_rank) !== null &&
          (target.usable || embeddingTargetNeedsInstall(target)),
      )
      .sort(compareRecommended)[0] ?? null
  );
}

/**
 * The line under the embedding Model label: where conversation text goes and
 * what indexing costs, or which Model to choose while none is.
 *
 * @param {object | null} target - The chosen target, or null.
 * @param {string} selectedId - The chosen target id ('' when none).
 * @param {(providerId: string) => string} providerName - Provider display name.
 * @param {object | null} recommended - `recommendedEmbeddingTarget(...)`.
 * @returns {{ text: string, attention: boolean }}
 */
export function describeEmbeddingModel(
  target,
  selectedId,
  providerName,
  recommended = null,
) {
  if (!selectedId) {
    return {
      text: recommended
        ? t('settings.recall.model.chooseRecommended', {
            model: recommended.label,
          })
        : t('settings.recall.model.choose'),
      attention: true,
    };
  }
  if (!target) {
    return { text: t('settings.recall.model.notOffered'), attention: true };
  }
  if (embeddingTargetNeedsInstall(target)) {
    return { text: t('settings.recall.model.installNeeded'), attention: true };
  }
  if (target.kind === 'local') {
    return { text: t('settings.recall.model.privacyLocal'), attention: false };
  }
  const provider = providerName(target.providerId) || target.providerId;
  if (targetIsLocal(target)) {
    return {
      text: t('settings.recall.model.privacyLocalRuntime', { provider }),
      attention: false,
    };
  }
  const price = finiteOrNull(target.facts?.input_price_per_million);
  return {
    text:
      price === null
        ? t('settings.recall.model.privacyProvider', { provider })
        : t('settings.recall.model.privacyProviderPrice', {
            provider,
            price: formatCost(price, activeLocaleTag()),
          }),
    attention: false,
  };
}

// What the installation dialog says about a Model before it is installed:
// its note and what installing fetches.
export function describeEmbeddingInstall(target) {
  return {
    note: textOrEmpty(target?.facts?.note),
    download: describeLocalModelDownload(target?.metadata),
  };
}

export function countText(value) {
  return new Intl.NumberFormat(activeLocaleTag()).format(value);
}

function tokenText(value) {
  return new Intl.NumberFormat(activeLocaleTag(), {
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(value);
}

function countOf(value) {
  return Math.max(0, finiteOrNull(value) ?? 0);
}

export function indexErrorText(error) {
  const code = textOrEmpty(error?.code);
  const generic = t('settings.recall.indexError.generic');
  return code ? tOr(`settings.recall.indexError.${code}`, generic) : generic;
}

function coverageText(state, indexed, total, waiting) {
  if (state === 'indexing') {
    return t('settings.recall.status.indexing', {
      indexed: countText(indexed),
      total: countText(total),
    });
  }
  if (total === 0) {
    return t('settings.recall.status.empty');
  }
  if (waiting === 0) {
    return t('settings.recall.status.complete', { count: countText(indexed) });
  }
  return t('settings.recall.status.progress', {
    indexed: countText(indexed),
    total: countText(total),
  });
}

// The time left while indexing, from the pass's measured rate: "about 12 min
// left", "about 2.5 hr left"; nothing until the server has measured it.
export function etaText(state, etaSeconds) {
  const seconds = finiteOrNull(etaSeconds);
  if (state !== 'indexing' || seconds === null || seconds <= 0) {
    return '';
  }
  if (seconds < 60) {
    return t('settings.recall.status.etaSoon');
  }
  const minutes = Math.round(seconds / 60);
  const [unit, value] =
    minutes < 90 ? ['minute', minutes] : ['hour', seconds / 3600];
  const duration = new Intl.NumberFormat(activeLocaleTag(), {
    style: 'unit',
    unit,
    unitDisplay: 'short',
    maximumFractionDigits: unit === 'hour' && value < 10 ? 1 : 0,
  }).format(value);
  return t('settings.recall.status.eta', { duration });
}

function waitingText(status, waiting) {
  if (waiting === 0) {
    return '';
  }
  const tokens = tokenText(countOf(status.estimate?.tokens));
  const cost = finiteOrNull(status.estimate?.cost);
  // A free Model (one on this computer) has no cost to name.
  return cost === null || cost === 0
    ? t('settings.recall.status.waiting', { tokens })
    : t('settings.recall.status.waitingCost', {
        tokens,
        cost: formatCost(cost, activeLocaleTag()),
      });
}

function spentText(spent) {
  if (countOf(spent?.requests) === 0) {
    return '';
  }
  const cost = finiteOrNull(spent.cost);
  if (cost === 0) {
    return '';
  }
  return cost === null
    ? t('settings.recall.status.spentTokens', {
        tokens: tokenText(countOf(spent.input_tokens)),
      })
    : t('settings.recall.status.spent', {
        cost: formatCost(cost, activeLocaleTag()),
      });
}

function problemText(status, state, nowMs) {
  if (state !== 'retrying' && state !== 'error') {
    return '';
  }
  const when = status.next_attempt_at
    ? formatRelativeTime(status.next_attempt_at, nowMs)
    : '';
  const parts = [indexErrorText(status.last_error)];
  if (when) {
    parts.push(
      state === 'retrying'
        ? t('settings.recall.status.retrying', { when })
        : t('settings.recall.status.nextAttempt', { when }),
    );
  }
  return parts.join(' ');
}

// The status line of the semantic index, or null while semantic search is
// off or has no embedding Model. `summary` describes coverage, the time left
// while indexing, and costs;
// `problem` is the translated failure with its next attempt while the index
// is retrying or failed.
export function describeRecallIndexStatus(status, nowMs = Date.now()) {
  const state = textOrEmpty(status?.state);
  if (!state || STATUS_LINE_HIDDEN_STATES.has(state)) {
    return null;
  }
  const indexed = countOf(status.indexed);
  const waiting = countOf(status.waiting);
  const skipped = countOf(status.skipped);
  const parts = [
    coverageText(state, indexed, indexed + waiting, waiting),
    etaText(state, status.eta_seconds),
    waitingText(status, waiting),
    skipped > 0
      ? t('settings.recall.status.skipped', { count: countText(skipped) })
      : '',
    spentText(status.spent),
  ].filter(Boolean);
  return {
    state,
    summary: parts.join(' · '),
    problem: problemText(status, state, nowMs),
  };
}
