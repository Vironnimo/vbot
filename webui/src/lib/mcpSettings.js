import { extensionOperation } from './api.js';
import { t, tOr } from './i18n.js';

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

// The resources of the changes the MCP Extension publishes: what `list`
// reports for a connection, and a management job that finished
// (`CONNECTIONS_RESOURCE` and `JOBS_RESOURCE` in
// `resources/extensions/mcp/_management.py`).
const LIVE_RESOURCES = new Set(['connections', 'jobs']);

// The advice for a failed connection's diagnosed `problem` (`status`
// reports its `code` and details); a code this WebUI does not know shows
// the Extension's own English advice.
export function mcpProblemText(problem) {
  if (!problem) return '';
  if (problem.code === 'command_not_found' && problem.requirement)
    return t('mcp.problemInstall', {
      command: problem.command,
      requirement: problem.requirement,
    });
  return tOr(`mcp.problem.${problem.code}`, problem.message ?? '', {
    command: problem.command,
    directory: problem.directory,
    credential: problem.credential,
    host: problem.host,
    status: problem.status,
    seconds: problem.seconds,
  });
}

// What importing the chosen servers of an import preview takes: the server
// names and their connection ids for the `import` operation, then the
// credential values the user typed for missing credentials, and the
// connections those values complete, which are enabled after saving.
// `values` holds the typed values by `mcpCredentialSlot`.
export function mcpImportPlan(servers, chosen, values = {}) {
  const plan = { servers: [], ids: {}, credentials: [], enable: [] };
  for (const server of servers) {
    if (!chosen.has(server.name) || server.error || server.conflict) continue;
    plan.servers.push(server.name);
    plan.ids[server.name] = server.id;
    const missing = server.credentials.filter(
      (credential) => credential.state === 'missing',
    );
    const typed = missing.filter(
      (credential) => values[mcpCredentialSlot(server, credential)],
    );
    for (const credential of typed)
      plan.credentials.push({
        id: server.id,
        key: credential.name,
        value: values[mcpCredentialSlot(server, credential)],
      });
    if (
      missing.length &&
      typed.length === missing.length &&
      !server.unresolved &&
      !server.disabled
    )
      plan.enable.push(server.id);
  }
  return plan;
}

// The key of a typed credential value: its server's name and its target.
export function mcpCredentialSlot(server, credential) {
  return `${server.name}\n${credential.target}`;
}

// This controller owns RPC reconciliation. The MCP Extension publishes a
// change whenever a connection's state or a job changes, so the panel reads
// `list` again only then (`handleInvalidation`), never on a timer. Drafts
// stay in the modal, so a status refresh can never replace a half-written
// connection or secret.
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
  let generation = 0;
  let inspectionGeneration = 0;
  // Whether the shown error is a failed read, which the next read replaces;
  // an action's error stays until the next action.
  let readFailed = false;
  // One read runs at a time; changes arriving meanwhile read once more after it.
  let reading = null;
  let readQueued = false;
  const publish = (patch) => {
    state = { ...state, ...patch };
    if (!disposed) onChange(state);
  };
  const invoke = (name, args = {}) => operation('mcp', name, args);
  async function refresh() {
    const request = ++generation;
    const current = () => !disposed && request === generation;
    try {
      if (state.job) {
        const result = await invoke('job', { job_id: state.job.job_id });
        if (!current()) return;
        if (result.state !== 'running')
          publish({ job: null, ...outcome(result) });
      }
      const result = await invoke('list');
      if (!current()) return;
      publish({
        connections: result.connections,
        loading: false,
        ...(readFailed ? { error: '' } : {}),
      });
      readFailed = false;
    } catch (error) {
      if (!current()) return;
      // A failure stays visible until the user retries or a change arrives.
      readFailed = true;
      publish({ error: error.message, loading: false });
    }
  }
  function outcome(job) {
    if (job.state === 'failed')
      return {
        error: job.error ?? job.result?.error?.message ?? t('mcp.testFailed'),
      };
    return {
      notice:
        job.state === 'cancelled'
          ? t('mcp.testCancelled')
          : t('mcp.testPassed', {
              checks: (job.result?.verified ?? []).join(', '),
            }),
    };
  }
  function refreshSoon() {
    readQueued = true;
    reading ??= (async () => {
      try {
        while (readQueued && !disposed) {
          readQueued = false;
          await refresh();
        }
      } finally {
        reading = null;
      }
    })();
    return reading;
  }
  async function act(work) {
    if (state.busy || disposed) return false;
    ++generation;
    readFailed = false;
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
    // An App Extension invalidation (`{owner, change}`): a change of the MCP
    // connections or jobs, or one without an owner (reconnect or Extension
    // reload), reads the connections again.
    handleInvalidation({ owner, change }) {
      if (
        owner == null ||
        (owner === 'mcp' && (!change || LIVE_RESOURCES.has(change.resource)))
      )
        void refreshSoon();
    },
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
    // The servers `source` (setup text from another client, a command line
    // or a URL) would import; saves nothing. `ids` replaces proposed
    // connection ids by server name.
    previewImport(source, ids = null) {
      return invoke('import', ids ? { source, ids } : { source });
    },
    // Saves the planned servers of `source` (see `mcpImportPlan`), stores the
    // typed credentials and enables the connections they complete. `saved`
    // tells whether the connections exist even when a later step failed.
    async importServers(source, plan) {
      let saved = false;
      const ok = await act(async () => {
        await invoke('import', {
          source,
          apply: true,
          servers: plan.servers,
          ids: plan.ids,
        });
        saved = true;
        for (const credential of plan.credentials)
          await invoke('credential', credential);
        for (const id of plan.enable) await invoke('enable', { id });
        publish({
          notice: t('mcp.imported', { count: plan.servers.length }),
        });
      });
      return { ok, saved };
    },
    dispose() {
      disposed = true;
      ++generation;
      ++inspectionGeneration;
    },
  };
}
