import { extensionOperation } from './api.js';
import { t } from './i18n.js';

export const MCP_REFRESH_MS = 3000;
const DEFAULT_TIMEOUT_SECONDS = 120;
export const MCP_DESCRIPTION_MAX_LENGTH = 200;
const clone = (value) => JSON.parse(JSON.stringify(value));
const mappings = [
  'environment',
  'credential_environment',
  'credential_headers',
];
// Settings of a pre-registered OAuth client; they exist only with OAuth.
const oauthClientFields = [
  'oauth_redirect_uri',
  'oauth_client_id',
  'oauth_client_secret',
  'oauth_scopes',
];
export const MCP_SAMPLING_POLICIES = ['off', 'ask', 'allow'];
export const MCP_ROOTS_POLICIES = ['off', 'workspace'];

export function mcpDraft(configuration = null) {
  const source = configuration ?? {
    id: '',
    transport: 'stdio',
    enabled: true,
  };
  return {
    ...clone(source),
    description: source.description ?? '',
    command: source.command ?? '',
    args: [...(source.args ?? [])],
    cwd: source.cwd ?? '',
    url: source.url ?? '',
    oauth: source.oauth ?? false,
    oauth_redirect_uri: source.oauth_redirect_uri ?? '',
    oauth_client_id: source.oauth_client_id ?? '',
    oauth_client_secret: source.oauth_client_secret ?? '',
    oauth_scopes: (source.oauth_scopes ?? []).join(' '),
    sampling: source.sampling ?? 'off',
    roots: source.roots ?? 'off',
    timeout: String(source.timeout ?? DEFAULT_TIMEOUT_SECONDS),
    ...Object.fromEntries(
      mappings.map((field) => [
        field,
        Object.entries(source[field] ?? {}).map(([name, value]) => ({
          name,
          value,
        })),
      ]),
    ),
  };
}

export function mcpConfiguration(draft) {
  const record = clone(draft);
  record.timeout = Number(draft.timeout);
  record.description = (draft.description ?? '').trim();
  if (!record.description) delete record.description;
  for (const field of mappings) {
    const entries = draft[field].map(({ name, value }) => [name.trim(), value]);
    if (
      entries.some(([name]) => !name) ||
      new Set(entries.map(([name]) => name)).size !== entries.length
    ) {
      throw new Error(t('mcp.mappingInvalid'));
    }
    record[field] = Object.fromEntries(entries);
  }
  record.oauth_client_id = (draft.oauth_client_id ?? '').trim();
  record.oauth_client_secret = (draft.oauth_client_secret ?? '').trim();
  record.oauth_scopes = [
    ...new Set((draft.oauth_scopes ?? '').split(/\s+/).filter(Boolean)),
  ];
  if (record.transport === 'stdio') {
    delete record.url;
    delete record.oauth;
  } else {
    delete record.command;
    delete record.args;
    delete record.cwd;
  }
  if (!record.oauth)
    for (const field of oauthClientFields) delete record[field];
  if (!record.cwd) delete record.cwd;
  for (const field of oauthClientFields)
    if (!record[field]?.length) delete record[field];
  return record;
}

export function mcpCredentialNames(configuration) {
  return [
    ...new Set([
      ...Object.values(configuration.credential_environment ?? {}),
      ...Object.values(configuration.credential_headers ?? {}),
      ...(configuration.oauth_client_secret
        ? [configuration.oauth_client_secret]
        : []),
    ]),
  ];
}

// This controller owns RPC reconciliation and polling. Drafts stay in the modal,
// so a status refresh can never replace a half-written connection or secret.
export function createMcpSettings({
  onChange,
  operation = extensionOperation,
}) {
  let state = {
    connections: [],
    loading: true,
    busy: false,
    error: '',
    notice: '',
    job: null,
    inspector: null,
  };
  let disposed = false;
  let timer;
  let generation = 0;
  let inspectionGeneration = 0;
  const publish = (patch) => {
    state = { ...state, ...patch };
    if (!disposed) onChange(state);
  };
  const invoke = (name, args = {}) => operation('mcp', name, args);
  const schedule = () => {
    clearTimeout(timer);
    if (!disposed) timer = setTimeout(() => void refresh(), MCP_REFRESH_MS);
  };
  async function refresh() {
    const request = ++generation;
    try {
      if (state.job) {
        const result = await invoke('job', { job_id: state.job.job_id });
        if (disposed || request !== generation) return;
        if (result.state !== 'running') {
          publish({ job: null });
          if (result.state === 'failed')
            throw new Error(
              result.error ??
                result.result?.error?.message ??
                t('mcp.testFailed'),
            );
          publish({
            notice:
              result.state === 'cancelled'
                ? t('mcp.testCancelled')
                : t('mcp.testPassed', {
                    checks: (result.result?.verified ?? []).join(', '),
                  }),
          });
        }
      }
      const result = await invoke('list');
      if (!disposed && request === generation)
        publish({ connections: result.connections, loading: false, error: '' });
    } catch (error) {
      if (!disposed && request === generation)
        publish({ error: error.message, loading: false });
      // A failure stays visible until the user retries; no silent retry loop.
      return;
    }
    if (!disposed && request === generation) schedule();
  }
  async function act(work) {
    if (state.busy || disposed) return false;
    clearTimeout(timer);
    ++generation;
    publish({ busy: true, error: '', notice: '' });
    try {
      await work();
      if (disposed) return false;
      await refresh();
      return true;
    } catch (error) {
      publish({ error: error.message });
      return false;
    } finally {
      publish({ busy: false });
    }
  }
  return {
    refresh,
    async inspect(id, { query = '', offset = 0 } = {}) {
      const request = ++inspectionGeneration;
      publish({
        inspector: { id, query, offset, loading: true, error: '', data: null },
      });
      try {
        const data = await invoke('inspect', { id, query, offset });
        if (!disposed && request === inspectionGeneration)
          publish({
            inspector: { id, query, offset, loading: false, error: '', data },
          });
      } catch (error) {
        if (!disposed && request === inspectionGeneration)
          publish({
            inspector: {
              id,
              query,
              offset,
              loading: false,
              error: error.message,
              data: null,
            },
          });
      }
    },
    closeInspector() {
      ++inspectionGeneration;
      publish({ inspector: null });
    },
    save(draft, original) {
      return act(async () => {
        const connection = mcpConfiguration(draft);
        const current = (await invoke('list')).connections.find(
          (item) => item.id === connection.id,
        );
        if (!original && current) throw new Error(t('mcp.duplicate'));
        if (
          original &&
          (!current ||
            JSON.stringify(current.configuration) !== JSON.stringify(original))
        )
          throw new Error(t('mcp.changed'));
        await invoke('save', { connection });
        publish({
          notice: t('mcp.saved'),
        });
      });
    },
    mutate(name, id) {
      return act(async () => {
        await invoke(name, { id });
      });
    },
    test(id) {
      return act(async () => {
        publish({ job: { ...(await invoke('test', { id })), connection: id } });
      });
    },
    cancel() {
      return act(async () => {
        if (state.job) await invoke('cancel-job', { job_id: state.job.job_id });
      });
    },
    credential(id, key, value) {
      return act(async () => {
        await invoke('credential', { id, key, value });
        publish({
          notice: value ? t('mcp.credentialSaved') : t('mcp.credentialCleared'),
        });
      });
    },
    dispose() {
      disposed = true;
      ++generation;
      ++inspectionGeneration;
      clearTimeout(timer);
    },
  };
}
