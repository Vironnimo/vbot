// Conversation search (Recall) settings: the backend choice behind the
// "Also search by meaning" switch, the recommended embedding Models with
// their facts, and the semantic index status line.

import { activeLocaleTag, t, tOr } from '../i18n.js';
import { formatCost } from '../statisticsView.js';
import { formatRelativeTime } from '../timeText.js';
import { describeLocalModelDownload } from './localModels.js';
import { textOrEmpty } from './values.js';

const RECALL_BACKEND_KEYWORD = 'sqlite_fts';
const RECALL_BACKEND_HYBRID = 'hybrid';

// The first-party backends that rank by meaning and therefore embed
// conversation text.
const MEANING_BACKENDS = Object.freeze(['vector', RECALL_BACKEND_HYBRID]);

// The two backends the switch selects; any other stored backend is shown in
// the Advanced backend list.
const SWITCH_BACKENDS = Object.freeze([
  RECALL_BACKEND_KEYWORD,
  RECALL_BACKEND_HYBRID,
]);

const RECALL_BACKEND_DEFAULTS = Object.freeze([
  RECALL_BACKEND_KEYWORD,
  'vector',
  RECALL_BACKEND_HYBRID,
]);

// Recommended Models shown per group before "All models".
const RECOMMENDED_PER_GROUP = 4;

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

export function buildRecallBackendOptions(recallSettings) {
  return normalizeRecallBackends(recallSettings?.available_backends).map(
    (backend) => ({
      value: backend,
      label: tOr(`settings.recall.backends.${backend}`, backend),
    }),
  );
}

// Whether the backend ranks by meaning, so it needs an embedding Model.
export function recallSearchesByMeaning(backend) {
  return MEANING_BACKENDS.includes(backend);
}

// The backend after the "Also search by meaning" switch changed: on keeps a
// backend that already searches by meaning and otherwise chooses keywords
// and meaning combined; off returns to keywords only.
export function recallBackendForMeaning(backend, on) {
  if (!on) {
    return RECALL_BACKEND_KEYWORD;
  }
  return recallSearchesByMeaning(backend) ? backend : RECALL_BACKEND_HYBRID;
}

// Whether the switch can turn meaning search on with the advertised backends.
export function recallMeaningAvailable(recallSettings) {
  return normalizeRecallBackends(recallSettings?.available_backends).includes(
    RECALL_BACKEND_HYBRID,
  );
}

// A backend the switch does not select (meaning only, or an Extension's)
// shows the Advanced backend list open.
export function recallBackendNeedsAdvanced(backend) {
  return !SWITCH_BACKENDS.includes(backend);
}

function finiteOrNull(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

// A target runs on this computer when vBot runs it itself or its Provider is
// a local runtime.
function targetIsLocal(target) {
  return target.kind === 'local' || target.facts?.local === true;
}

function compareRecommended(left, right) {
  const leftRank = finiteOrNull(left.facts?.recommended_rank) ?? Infinity;
  const rightRank = finiteOrNull(right.facts?.recommended_rank) ?? Infinity;
  if (leftRank !== rightRank) {
    return leftRank - rightRank;
  }
  return left.label.localeCompare(right.label);
}

function embeddingChoice(target) {
  const local = targetIsLocal(target);
  const multilingual = target.facts?.multilingual;
  const price = finiteOrNull(target.facts?.input_price_per_million);
  const facts = [];
  if (multilingual === true) {
    facts.push(t('settings.recall.model.factMultilingual'));
  } else if (multilingual === false) {
    facts.push(t('settings.recall.model.factEnglish'));
  }
  // A Model on this computer reports no price or a zero price.
  if (local && (price === null || price === 0)) {
    facts.push(t('settings.recall.model.factLocal'));
  } else if (price === null) {
    facts.push(t('settings.recall.model.factPriceUnknown'));
  } else {
    facts.push(
      t('settings.recall.model.factPrice', {
        price: formatCost(price, activeLocaleTag()),
      }),
    );
  }
  const usable = target.usable !== false;
  // A Model vBot runs itself is installed on request; `download` names what
  // installing it fetches.
  const installable = target.kind === 'local' && !usable;
  return {
    id: target.id,
    label: target.label,
    providerId: target.providerId ?? '',
    local,
    usable,
    installable,
    download: installable ? describeLocalModelDownload(target.metadata) : '',
    facts,
    note: textOrEmpty(target.facts?.note),
  };
}

// The short recommended list: targets vBot runs itself and targets with a
// recommendation rank, best first, split into "On this computer" and
// "Cloud". The selected target always stays visible in its group.
export function buildEmbeddingModelChoices(targets, selectedId = '') {
  const list = Array.isArray(targets) ? targets : [];
  const recommended = list
    .filter(
      (target) =>
        target.kind === 'local' ||
        finiteOrNull(target.facts?.recommended_rank) !== null,
    )
    .sort(compareRecommended);
  const groups = { local: [], cloud: [] };
  for (const target of recommended) {
    const group = targetIsLocal(target) ? groups.local : groups.cloud;
    if (group.length < RECOMMENDED_PER_GROUP) {
      group.push(target);
    }
  }
  const selected = list.find((target) => target.id === selectedId);
  if (
    selected &&
    !groups.local.includes(selected) &&
    !groups.cloud.includes(selected)
  ) {
    (targetIsLocal(selected) ? groups.local : groups.cloud).push(selected);
  }
  return {
    local: groups.local.map(embeddingChoice),
    cloud: groups.cloud.map(embeddingChoice),
  };
}

// Where the chosen target sends conversation text. `providerName` resolves a
// Provider id to its display name.
export function describeEmbeddingPrivacy(target, providerName) {
  if (!target) {
    return t('settings.recall.model.privacyUnknown');
  }
  if (target.kind === 'local') {
    return t('settings.recall.model.privacyLocal');
  }
  return t('settings.recall.model.privacyProvider', {
    provider: providerName(target.providerId) || target.providerId,
  });
}

function countText(value) {
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

function indexErrorText(error) {
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
function etaText(state, etaSeconds) {
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
  return cost === null
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
