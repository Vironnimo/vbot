import { t } from '../i18n.js';

// History has stable sequences, live rows acquire them on reconciliation.
function searchMessageOf(item) {
  if (item.type === 'assistant_output')
    return (
      item.messages?.[0] ??
      (item.messageId ? { id: item.messageId, role: 'assistant' } : null)
    );
  if (['message', 'external_history', 'user_message'].includes(item.type))
    return item.message;
  return null;
}

function matchesHit(item, hit) {
  const message = searchMessageOf(item);
  return (
    message?.id === hit.message_id &&
    message.role === hit.role &&
    (message.history_sequence == null ||
      message.history_sequence === hit.sequence)
  );
}

export function locateSearchHit(rows, hit) {
  for (const row of rows) {
    if (matchesHit(row, hit)) return { row, message: row.message };
    if (
      row.type !== 'assistant_run' ||
      (hit.run_id && row.runId !== hit.run_id)
    )
      continue;
    const child = row.items.find((item) => matchesHit(item, hit));
    if (child) return { row, child, message: searchMessageOf(child) };
  }
  return null;
}

// A stable dependency across token deltas; only canonical messages get ids.
export function latestSearchMessage(items) {
  for (let i = items.length - 1; i >= 0; i--) {
    const row = items[i];
    const child = row.items?.findLast((item) => searchMessageOf(item)?.id);
    const message = searchMessageOf(child ?? row);
    if (message?.id) return message.id;
  }
  return '';
}

export function createSessionSearch({ operations, history, errorMessage }) {
  let session = null;
  let key = $state('');
  let query = $state('');
  let hits = $state.raw([]);
  let complete = $state(true);
  let selected = $state.raw(null);
  let target = $state.raw(null);
  let searching = $state(false);
  let jumping = $state(false);
  let error = $state('');
  let timer;
  let request = 0;
  let jump = 0;

  function clearJump() {
    jump++;
    jumping = false;
    selected = null;
    target = null;
  }

  function close() {
    clearTimeout(timer);
    request++;
    session = null;
    key = '';
    query = '';
    hits = [];
    complete = true;
    searching = false;
    error = '';
    clearJump();
  }

  function schedule(immediate = false) {
    clearTimeout(timer);
    const version = ++request;
    const term = query.trim();
    if (!session || !term) return;
    searching = true;
    async function read() {
      try {
        const result = await operations.searchChat({
          agent_id: session.agentId,
          session_id: session.sessionId,
          query: term,
        });
        if (request !== version) return;
        hits = result.hits;
        complete = result.complete;
        if (
          selected &&
          !hits.some(
            (hit) =>
              hit.sequence === selected.sequence &&
              hit.message_id === selected.message_id,
          )
        )
          clearJump();
        error = '';
      } catch (cause) {
        if (version === request) error = errorMessage(cause);
      } finally {
        if (version === request) searching = false;
      }
    }
    if (immediate) return read();
    timer = setTimeout(read, 250);
  }

  async function select(hit, reveal) {
    const version = ++jump;
    const current = () => version === jump;
    const source = session;
    if (!source || !hit) return false;
    selected = hit;
    jumping = true;
    error = '';
    try {
      while (current()) {
        const rows = source.timelineItems;
        if (locateSearchHit(rows, hit)) {
          const destination = { hit, query };
          target = destination;
          if (await reveal(destination, current)) return current();
          break;
        }
        const run =
          hit.run_id &&
          rows.find(
            (item) =>
              item.type === 'assistant_run' && item.runId === hit.run_id,
          );
        const gap = run?.items?.find((item) => item.type === 'history_gap');
        if (gap) {
          if (!(await history.loadRunHistory(source, gap))) break;
        } else if (
          run ||
          !source.hasOlderHistory ||
          !(await history.loadOlderHistory(source, 500))
        )
          break;
      }
      if (current()) {
        target = null;
        error = source.actionError || t('chat.search.unavailable');
      }
      return false;
    } finally {
      if (current()) jumping = false;
    }
  }

  return {
    get key() {
      return key;
    },
    get query() {
      return query;
    },
    get hits() {
      return hits;
    },
    get complete() {
      return complete;
    },
    get selected() {
      return selected;
    },
    get target() {
      return target;
    },
    get searching() {
      return searching;
    },
    get jumping() {
      return jumping;
    },
    get error() {
      return error;
    },
    open(source) {
      if (session !== source) close();
      session = source;
      key = source.key;
    },
    setQuery(value) {
      if (value === query) return;
      query = value;
      hits = [];
      complete = true;
      searching = false;
      error = '';
      clearJump();
      schedule();
    },
    refresh: schedule,
    select,
    close,
  };
}
