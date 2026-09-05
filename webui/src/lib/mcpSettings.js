import {
  buildAgentTargetOptions,
  projectIdsFromList,
  projectTeamEntry,
} from './agentTargetOptions.js';
import {
  extensionOperation,
  listAgents,
  listProjects,
  showProject,
} from './api.js';
import { t } from './i18n.js';

export const MCP_REFRESH_MS = 3000;
const DEFAULT_TIMEOUT_SECONDS = 120;
const clone = (value) => JSON.parse(JSON.stringify(value));
const mappings = [
  'environment',
  'credential_environment',
  'credential_headers',
];

export function mcpDraft(configuration = null) {
  const source = configuration ?? {
    id: '',
    transport: 'stdio',
    agents: [],
    enabled: true,
  };
  return {
    ...clone(source),
    command: source.command ?? '',
    args: [...(source.args ?? [])],
    cwd: source.cwd ?? '',
    url: source.url ?? '',
    oauth: source.oauth ?? false,
    oauth_redirect_uri: source.oauth_redirect_uri ?? '',
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
  for (const field of mappings) {
    const entries = draft[field].map(({ name, value }) => [name.trim(), value]);
    if (
      entries.some(([name]) => !name) ||
      new Set(entries.map(([name]) => name)).size !== entries.length
    ) {
      throw new Error(
        t('mcp.mappingInvalid', 'Each entry needs a unique, non-empty name.'),
      );
    }
    record[field] = Object.fromEntries(entries);
  }
  if (record.transport === 'stdio') {
    delete record.url;
    delete record.oauth;
    delete record.oauth_redirect_uri;
  } else {
    delete record.command;
    delete record.args;
    delete record.cwd;
    if (!record.oauth) delete record.oauth_redirect_uri;
  }
  if (!record.cwd) delete record.cwd;
  if (!record.oauth_redirect_uri) delete record.oauth_redirect_uri;
  return record;
}

export function mcpCredentialNames(configuration) {
  return [
    ...new Set([
      ...Object.values(configuration.credential_environment ?? {}),
      ...Object.values(configuration.credential_headers ?? {}),
    ]),
  ];
}

// This controller owns RPC reconciliation and polling. Drafts stay in the modal,
// so a status refresh can never replace a half-written connection or secret.
export function createMcpSettings({
  onChange,
  operation = extensionOperation,
  agents = listAgents,
  projects = listProjects,
  project = showProject,
}) {
  let state = {
    connections: [],
    targets: [],
    loading: true,
    busy: false,
    error: '',
    targetError: '',
    notice: '',
    job: null,
  };
  let disposed = false;
  let timer;
  let generation = 0;
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
                t('mcp.testFailed', 'Connection test failed.'),
            );
          publish({
            notice:
              result.state === 'cancelled'
                ? t('mcp.testCancelled', 'Connection test cancelled.')
                : t('mcp.testPassed', 'Connection test passed: {checks}.', {
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
    async loadTargets() {
      try {
        const [identities, catalog] = await Promise.all([agents(), projects()]);
        const results = await Promise.allSettled(
          projectIdsFromList(catalog).map(async (id) =>
            projectTeamEntry(id, await project(id)),
          ),
        );
        const failed = results.filter((result) => result.status === 'rejected');
        publish({
          targets: buildAgentTargetOptions(
            identities.agents,
            results
              .filter((result) => result.status === 'fulfilled')
              .map((result) => result.value),
          ),
          targetError: failed.length
            ? t(
                'mcp.targetsPartial',
                'Some Project Agents could not be loaded. Existing grants are preserved.',
              )
            : '',
        });
      } catch (error) {
        publish({ targetError: error.message });
      }
    },
    save(draft, original) {
      return act(async () => {
        const connection = mcpConfiguration(draft);
        const current = (await invoke('list')).connections.find(
          (item) => item.id === connection.id,
        );
        if (!original && current)
          throw new Error(
            t(
              'mcp.duplicate',
              'This connection already exists. Choose another name or edit the existing connection.',
            ),
          );
        if (
          original &&
          (!current ||
            JSON.stringify(current.configuration) !== JSON.stringify(original))
        )
          throw new Error(
            t(
              'mcp.changed',
              'This connection changed elsewhere. Close the editor and reopen it before saving.',
            ),
          );
        await invoke('save', { connection });
        publish({
          notice: t(
            'mcp.saved',
            'Connection saved. Test it to verify access to the server.',
          ),
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
          notice: value
            ? t('mcp.credentialSaved', 'Credential saved.')
            : t('mcp.credentialCleared', 'Credential cleared.'),
        });
      });
    },
    dispose() {
      disposed = true;
      ++generation;
      clearTimeout(timer);
    },
  };
}
