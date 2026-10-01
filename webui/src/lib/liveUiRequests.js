import { isPlainObject } from './values.js';

// UI requests the server sends a Live call's owner over its socket: the page
// validates each one and runs it through the app's UI actions (`open`,
// `terminalView`). See `server/live/_tools.py` for the request shapes.

const OPEN_VIEWS = new Set([
  'chat',
  'terminals',
  'agents',
  'projects',
  'calendar',
  'cron',
  'skills',
  'settings',
  'statistics',
  'logs',
]);
// The ids that name one item of a view in an open request.
const OPEN_TARGET_IDS = {
  chat: ['agent_id', 'session_id'],
  agents: ['agent_id'],
  projects: ['project_id'],
};
const TERMINAL_VIEW_OPS = new Set([
  'refresh',
  'show',
  'maximize',
  'restore',
  'show_group',
]);
const ERROR_CODE_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;

const isText = (value) => typeof value === 'string' && value.length > 0;
const failure = (code) => Object.assign(new Error(code), { code });

// UI owners report failures as `{code}` errors or plain `Error('<code>')`.
export function uiErrorCode(error) {
  for (const candidate of [error?.code, error?.message]) {
    if (typeof candidate === 'string' && ERROR_CODE_PATTERN.test(candidate))
      return candidate;
  }
  return 'operation_failed';
}

function uiAction(uiActions, name) {
  const action = uiActions[name];
  if (typeof action !== 'function') throw failure('unsupported_action');
  return action;
}

// An open request shows a view alone or exactly one item of it: a Chat
// Session (agent_id and session_id), an Agent page, or a Project page.
function openTarget(args) {
  const { view } = args;
  const ids = {
    agent_id: args.agent_id ?? undefined,
    session_id: args.session_id ?? undefined,
    project_id: args.project_id ?? undefined,
  };
  const given = Object.keys(ids).filter((key) => ids[key] !== undefined);
  if (!given.length) return { view };
  const expected = OPEN_TARGET_IDS[view] ?? [];
  if (
    given.length !== expected.length ||
    !expected.every((key) => isText(ids[key]))
  )
    throw failure('invalid_arguments');
  return Object.fromEntries([
    ['view', view],
    ...expected.map((key) => [key, ids[key]]),
  ]);
}

// Validates one UI request and returns the operation that executes it; an
// invalid request throws an error with its `code`.
export function liveUiOperation(uiActions, actionName, rawArgs, guard) {
  const args = rawArgs ?? {};
  if (!isPlainObject(args)) throw failure('invalid_arguments');
  if (actionName === 'open') {
    if (!OPEN_VIEWS.has(args.view)) throw failure('invalid_view');
    const target = openTarget(args);
    const open = uiAction(uiActions, 'open');
    return async () => ({ applied: (await open(target, guard)) !== false });
  }
  if (actionName === 'terminal_view') {
    const { op } = args;
    if (!TERMINAL_VIEW_OPS.has(op)) throw failure('invalid_arguments');
    const target = { op };
    if (op === 'show' || op === 'maximize') {
      if (!isText(args.terminal_id)) throw failure('invalid_arguments');
      target.terminal_id = args.terminal_id;
    } else if (op === 'show_group') {
      if (!isText(args.group_id)) throw failure('invalid_arguments');
      target.group_id = args.group_id;
    }
    const terminalView = uiAction(uiActions, 'terminalView');
    return () => terminalView(target, guard);
  }
  throw failure('unsupported_action');
}
