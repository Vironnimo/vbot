import {
  formatMoment,
  formatRelativeTime,
} from '../../../../webui/src/lib/timeText.js';
import { activeLocaleTag, t } from '../../../../webui/src/lib/i18n.js';
import { modelIdParts } from '../../../../webui/src/lib/modelSelection.js';
import { participantCount, participantTotal } from './pagePresentation.js';

// How the navigation pane, the Run header and the Run dialogs present Runs
// and Swarms (profiles): titles, states in words and colour, summaries,
// details cards and the changes of a Run's communication settings.

// A placeholder value that marks where an element (a time, a button) stands
// in a translation: `aroundSlot(t(key, { time: SLOT }))` splits the text there
// into `[before, after]`.
export const SLOT = '\u0000';

export function aroundSlot(text) {
  const [before, after = ''] = text.split(SLOT);
  return [before, after];
}

// `count` list items in the locale's words ("A, B and C") as parts, so each
// item can be its own element: `{ item }` stands for the item at that index,
// `{ text }` for the words between items.
export function listParts(count) {
  const items = Array.from({ length: count }, (_, index) => String(index));
  try {
    return new Intl.ListFormat(activeLocaleTag(), {
      style: 'long',
      type: 'conjunction',
    })
      .formatToParts(items)
      .map((part) =>
        part.type === 'element'
          ? { item: Number(part.value) }
          : { text: part.value },
      );
  } catch {
    return items.flatMap((_, index) =>
      index ? [{ text: ', ' }, { item: index }] : [{ item: index }],
    );
  }
}

// A Swarm's Models with their participant counts, in the order the Swarm
// first names them.
function profileModels(profile) {
  const perModel = new Map();
  for (const row of profile.participants ?? [])
    perModel.set(row.model, (perModel.get(row.model) ?? 0) + row.count);
  return [...perModel];
}

// A Swarm row's second line: its size, then its Model's own name or how many
// Models it mixes.
export function profileSummary(profile) {
  const size = participantCount(participantTotal(profile));
  const models = profileModels(profile);
  if (!models.length) return size;
  return t('swarm.sidebar.profileSummary', {
    size,
    models:
      models.length === 1
        ? modelIdParts(models[0][0]).name
        : t('swarm.sidebar.modelCount', { count: models.length }),
  });
}

// A Swarm row's details card: its participants per Model, each Model id with
// its provider path muted as ModelId shows it.
export function profileTooltip(profile) {
  return {
    title: profile.name,
    rows: profileModels(profile).map(([model, count]) => {
      const { lead, name } = modelIdParts(model);
      return { label: participantCount(count), lead, value: name };
    }),
    placement: 'right',
    alignTo: '.swarm-row__name',
  };
}

// Markdown a goal's first line may start with (a heading, quote or list
// marker) and the inline Markdown it may carry; code spans keep their text.
const LINE_MARKERS = /^\s{0,3}(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)+/u;
const INLINE_MARKDOWN = [
  [/!?\[([^\]]*)\]\([^)]*\)/gu, '$1'],
  [/(\*\*|__)(\S(?:.*?\S)?)\1/gu, '$2'],
  [/~~(\S(?:.*?\S)?)~~/gu, '$1'],
  [/\*(\S(?:.*?\S)?)\*/gu, '$1'],
  [/(^|\W)_(\S(?:.*?\S)?)_(?=\W|$)/gu, '$1$2'],
];

// A Run's title: the first line of its goal as plain text. A list entry
// carries its title as `title`, a Run's detail the whole goal as `prompt`.
export function runTitle(swarm) {
  const line = (swarm.title ?? swarm.prompt ?? '')
    .split(/\r?\n/u)[0]
    .replace(LINE_MARKERS, '');
  const title = line
    .split(/(`[^`]*`)/u)
    .map((part, index) =>
      index % 2
        ? part.slice(1, -1)
        : INLINE_MARKDOWN.reduce(
            (text, [pattern, replacement]) =>
              text.replace(pattern, replacement),
            part,
          ),
    )
    .join('')
    .trim();
  return title || swarm.id;
}

const RUN_STATES = {
  preparing: () => t('swarm.state.preparing'),
  running: () => t('swarm.state.running'),
  idle: () => t('swarm.state.idle'),
  needs_attention: () => t('swarm.state.needsAttention'),
  stopping: () => t('swarm.state.stopping'),
  stopped: () => t('swarm.state.stopped'),
  cancelled: () => t('swarm.state.stopped'),
  interrupted: () => t('swarm.state.interrupted'),
  deleting: () => t('swarm.state.deleting'),
};

// A Run state's StatusChip variant: amber while the Run works or was
// interrupted, green while it is open and waits for messages, red while it
// needs the user; stopped Runs stay neutral.
const RUN_STATE_VARIANTS = {
  preparing: 'warn',
  running: 'warn',
  stopping: 'warn',
  interrupted: 'warn',
  idle: 'success',
  needs_attention: 'error',
};

const VARIANT_TONES = { warn: 'warning', success: 'success', error: 'danger' };

// A Run state in words; a state this page does not know shows as stored.
export const runStateLabel = (state) =>
  Object.hasOwn(RUN_STATES, state) ? RUN_STATES[state]() : String(state ?? '');

export const runStateVariant = (state) =>
  RUN_STATE_VARIANTS[state] ?? 'neutral';

// The states of an open Run: it works, waits for messages or needs the user.
export const openRunStates = Object.freeze([
  'preparing',
  'running',
  'stopping',
  'idle',
  'needs_attention',
]);

// The states in which a Run accepts new communication settings.
export const settingsRunStates = Object.freeze([
  'preparing',
  'running',
  'idle',
  'needs_attention',
]);

// The name of the Swarm a Run started from; `swarm` is a list entry or a
// detail.
export const runProfileName = (swarm) =>
  swarm.name || swarm.profile_snapshot?.name || t('swarm.profile');

// How many participants a Run has, in words.
export const runSize = (swarm) =>
  participantCount(swarm.participant_count ?? swarm.participants?.length ?? 0);

// The Run header's subtitle around its start time, which shows as a
// separate element with the absolute moment as its tooltip: "Research ·
// started 2 hours ago · 6 participants". `time` is '' before the Run has a
// start time.
export function runSummary(swarm, nowMs = Date.now()) {
  const name = runProfileName(swarm);
  const size = runSize(swarm);
  if (!swarm.created_at)
    return {
      before: t('swarm.run.summaryUnstarted', {
        swarm: name,
        participants: size,
      }),
      time: '',
      after: '',
      moment: '',
    };
  const [before, after] = aroundSlot(
    t('swarm.run.summary', { swarm: name, participants: size, time: SLOT }),
  );
  return {
    before,
    time: formatRelativeTime(swarm.created_at, nowMs),
    after,
    moment: formatMoment(swarm.created_at, { nowMs }),
  };
}

// A Run row's second line: its state and how long ago it started.
export const runRowDetail = (swarm, nowMs = Date.now()) =>
  swarm.created_at
    ? t('swarm.sidebar.runDetail', {
        state: runStateLabel(swarm.state),
        time: formatRelativeTime(swarm.created_at, nowMs),
      })
    : runStateLabel(swarm.state);

// What a Run's confirmation names besides its title: when it started and its
// size.
export const runTarget = (swarm, nowMs = Date.now()) =>
  swarm.created_at
    ? t('swarm.run.target', {
        time: formatMoment(swarm.created_at, { nowMs }),
        participants: runSize(swarm),
      })
    : runSize(swarm);

// A Run row's details card: the whole title, its Swarm, state, start moment
// and size.
export function runTooltip(swarm, nowMs = Date.now()) {
  return {
    title: runTitle(swarm),
    rows: [
      { label: t('swarm.profile'), value: runProfileName(swarm) },
      {
        label: t('swarm.details.state'),
        value: runStateLabel(swarm.state),
        tone: VARIANT_TONES[runStateVariant(swarm.state)] ?? '',
      },
      {
        label: t('swarm.details.started'),
        value: swarm.created_at
          ? formatMoment(swarm.created_at, { nowMs })
          : '',
      },
      { label: t('swarm.participants'), value: runSize(swarm) },
    ],
    placement: 'right',
    alignTo: '.swarm-row__name',
  };
}

// The batching limits of a Run's communication settings: their labels in
// change lines and validation, their allowed range and how a value reads.
const number = (value) =>
  new Intl.NumberFormat(activeLocaleTag()).format(value);
export const DELIVERY_LIMITS = Object.freeze([
  {
    field: 'coalesce_ms',
    id: 'coalesce',
    label: () => t('swarm.communication.batchDelay'),
    min: 0,
    max: 5000,
    format: (value) => t('swarm.communication.ms', { value: number(value) }),
  },
  {
    field: 'batch_messages',
    id: 'batch-messages',
    label: () => t('swarm.delivery.batchMessages'),
    min: 1,
    max: 100,
    format: number,
  },
  {
    field: 'batch_chars',
    id: 'batch-chars',
    label: () => t('swarm.delivery.batchChars'),
    min: 16000,
    max: 128000,
    format: number,
  },
]);

// The message that names a batching limit outside its range.
export const deliveryLimitError = (limit) =>
  t('swarm.communication.invalidNumber', {
    field: limit.label(),
    min: number(limit.min),
    max: number(limit.max),
  });

// A `delivery` draft as the settings fields hand it back (batching limits as
// typed) with its limits as numbers (an empty field as null), and the limits
// that are no whole number in their range.
export function normalizeDelivery(draft) {
  const value = JSON.parse(JSON.stringify(draft));
  const invalid = [];
  for (const limit of DELIVERY_LIMITS) {
    const text = String(value[limit.field] ?? '').trim();
    const next = text === '' ? null : Number(text);
    value[limit.field] = next;
    if (!Number.isInteger(next) || next < limit.min || next > limit.max)
      invalid.push(limit);
  }
  return { value, invalid };
}

// The delivery routes and receive modes as the settings fields name them.
const ROUTE_LABELS = {
  main: () => t('swarm.delivery.route.main'),
  discussion: () => t('swarm.delivery.route.discussion'),
  ping: () => t('swarm.delivery.route.ping'),
};
const MODE_LABELS = {
  all: () => t('swarm.delivery.receive.all'),
  idle: () => t('swarm.delivery.receive.idle'),
  pull: () => t('swarm.delivery.receive.pull'),
};
const modeLabel = (mode) =>
  Object.hasOwn(MODE_LABELS, mode) ? MODE_LABELS[mode]() : String(mode);
const onOff = (value) =>
  value ? t('swarm.communication.on') : t('swarm.communication.off');

// The changes from one `delivery` value to another, one readable line each:
// "Main discussion · Receive messages: When idle → On request or when
// waking", "Batch delay: 250 ms → 500 ms". `after` holds the limits as
// numbers; the `invalid` limits (from `normalizeDelivery`) show as errors
// instead.
export function deliveryChanges(before, after, invalid = []) {
  if (!before || !after) return [];
  const lines = [];
  for (const [route, routeLabel] of Object.entries(ROUTE_LABELS)) {
    const was = before[route] ?? {};
    const next = after[route] ?? {};
    if (was.mode !== next.mode)
      lines.push({
        id: `${route}-mode`,
        text: t('swarm.communication.routeChange', {
          route: routeLabel(),
          setting: t('swarm.delivery.receive'),
          before: modeLabel(was.mode),
          after: modeLabel(next.mode),
        }),
      });
    if (was.wake_idle !== next.wake_idle)
      lines.push({
        id: `${route}-wake`,
        text: t('swarm.communication.routeChange', {
          route: routeLabel(),
          setting: t('swarm.delivery.wakeIdle'),
          before: onOff(was.wake_idle),
          after: onOff(next.wake_idle),
        }),
      });
  }
  for (const limit of DELIVERY_LIMITS) {
    if (invalid.includes(limit) || before[limit.field] === after[limit.field])
      continue;
    lines.push({
      id: limit.field,
      text: t('swarm.communication.change', {
        setting: limit.label(),
        before: limit.format(before[limit.field]),
        after: limit.format(after[limit.field]),
      }),
    });
  }
  return lines;
}
