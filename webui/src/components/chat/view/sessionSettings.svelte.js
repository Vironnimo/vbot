import { untrack } from 'svelte';

import {
  effortOptionsForReasoning,
  reasoningForModelValue,
} from '$lib/agentForm.js';
import { parseAgentAddress } from '$lib/agentAddress.js';
import { t } from '$lib/i18n.js';
import { takeSessionInvalidations } from '$lib/sessionInvalidation.js';
import { renameAgentInKey, renameAgentInKeys } from '$lib/chatState.js';

// The Session settings the composer footer shows for the displayed
// conversation: the Project it works in, and the Model and thinking effort it
// uses instead of its Agent's (Session Agent overrides; the Agent itself never
// changes).
//
// - A draft holds its choices per draft key, so each Chat area keeps them
//   across navigation like the draft text. Its first message sends them as
//   the new Session's `working_project_id` and `agent_overrides`; the next
//   draft then starts from the defaults again.
// - An existing Session reads its Project and overrides from its `session.get`
//   row and writes a Model or effort change through
//   `session.set_agent_overrides`. A change shows at once and settles to the
//   server's answer.
//
// Defaults come from the Agent: an Identity Agent's `agent.list` row, or the
// Team member's effective values from `project.show`.
export function createSessionSettings(context) {
  // draft key -> { workingProjectId?, model, thinking_effort }. An absent
  // `workingProjectId` is the Agent's default Project; `null` the Workspace.
  let drafts = $state({});
  // Session key -> { workingProjectId, projectKnown, overrides } as last read
  // or written.
  let rows = $state({});
  // Session key -> override fields being written ({ model: null } clears).
  let pending = $state({});
  // The Model catalog (`{ models, connections }`), null until it answers.
  let catalog = $state.raw(null);
  let catalogFailed = $state(false);
  let catalogRequested = false;
  let catalogRequestId = 0;

  // Bumped when a Session invalidation names the displayed Session.
  let rowRevision = $state(0);
  let lastInvalidationId = null;
  let requestedRowKey = '';
  // Counts override writes as they start and end: a row read that overlaps
  // one may predate it, so its answer is dropped.
  let overrideWrites = 0;
  // Session key -> the id of its latest override write.
  const latestWrite = {};

  // What the footer is about: a draft or an existing Session, else nothing.
  let current = $derived.by(() => {
    if (!context.target.activeAgent) {
      return null;
    }
    const draft = context.target.activeDraft();
    if (draft) {
      return {
        kind: 'draft',
        key: draft.key,
        agentAddress: draft.agentAddress,
        sessionId: '',
      };
    }
    const { agentAddress, sessionId } = context.target.activeAddressing();
    if (!agentAddress || !sessionId) {
      return null;
    }
    return {
      kind: 'session',
      key: `${agentAddress}::${sessionId}`,
      agentAddress,
      sessionId,
    };
  });

  let view = $derived.by(() => {
    const shown = current;
    if (!shown) {
      return null;
    }
    const defaults = agentDefaults(shown.agentAddress);
    const { projectId: teamProjectId } = parseAgentAddress(shown.agentAddress);
    const base = {
      kind: shown.kind,
      catalog,
      catalogFailed,
      defaultModel: defaults.model,
      defaultThinkingEffort: defaults.thinkingEffort,
    };
    const teamProject = teamProjectId
      ? { editable: false, known: true, team: true, value: teamProjectId }
      : null;
    if (shown.kind === 'draft') {
      const draft = drafts[shown.key] ?? {};
      return {
        ...base,
        project: teamProject ?? {
          editable: true,
          known: true,
          team: false,
          value:
            draft.workingProjectId !== undefined
              ? draft.workingProjectId
              : defaults.rootProjectId,
          defaultValue: defaults.rootProjectId,
        },
        model: draft.model ?? '',
        thinkingEffort: draft.thinking_effort ?? '',
      };
    }
    const row = rows[shown.key];
    const changes = pending[shown.key] ?? {};
    const stored = row?.overrides ?? {};
    const override = (field) =>
      Object.hasOwn(changes, field)
        ? (changes[field] ?? '')
        : text(stored[field]);
    return {
      ...base,
      project: teamProject ?? {
        editable: false,
        known: row?.projectKnown === true,
        team: false,
        value: row?.workingProjectId ?? null,
      },
      model: override('model'),
      thinkingEffort: override('thinking_effort'),
    };
  });

  function agentDefaults(agentAddress) {
    const { agentId, projectId } = parseAgentAddress(agentAddress);
    if (projectId) {
      const member =
        projectId === context.selectedProjectId
          ? context.target.projectTeam.find(
              (candidate) => candidate.agent_id === agentId,
            )
          : null;
      return {
        model: text(member?.effective?.model?.value),
        thinkingEffort: text(member?.effective?.thinking_effort?.value),
        rootProjectId: projectId,
      };
    }
    const agent = context.target.agentById(agentId);
    return {
      model: text(agent?.model),
      thinkingEffort: text(agent?.thinking_effort),
      rootProjectId: text(agent?.root_project_id) || null,
    };
  }

  // The Model catalog is read once the footer first shows, then again
  // whenever the Model picker opens.
  $effect(() => {
    if (!context.active || !current || catalogRequested) {
      return;
    }
    catalogRequested = true;
    untrack(() => void loadCatalog().catch(() => {}));
  });

  async function loadCatalog() {
    const requestId = ++catalogRequestId;
    try {
      const result = await context.loadModelCatalog();
      if (requestId === catalogRequestId) {
        catalog = result;
        catalogFailed = false;
      }
      return result;
    } catch (error) {
      if (requestId === catalogRequestId && !catalog) {
        catalogFailed = true;
      }
      throw error;
    }
  }

  $effect(() => {
    const entries = context.sessionInvalidations;
    untrack(() => {
      const { targets, lastId, overflowed } = takeSessionInvalidations(
        entries,
        lastInvalidationId,
      );
      lastInvalidationId = lastId;
      const shown = current;
      if (
        shown?.kind === 'session' &&
        (overflowed || targets.some((target) => namesSession(target, shown)))
      ) {
        rowRevision += 1;
      }
    });
  });

  function namesSession(target, shown) {
    if (target.all) {
      return true;
    }
    if (target.renamedAgent) {
      return (
        target.renamedAgent.oldAgentId === shown.agentAddress ||
        target.renamedAgent.newAgentId === shown.agentAddress
      );
    }
    // Run completion and read acknowledgement leave the settings unchanged.
    if (target.runId || target.readRunId) {
      return false;
    }
    return (
      target.agentAddress === shown.agentAddress &&
      target.sessionId === shown.sessionId
    );
  }

  $effect(() => {
    const shown = current;
    const requestKey =
      shown?.kind === 'session'
        ? `${shown.key}::${context.sessionsRefreshToken}::${rowRevision}`
        : '';
    untrack(() => {
      if (!requestKey || requestKey === requestedRowKey) {
        return;
      }
      requestedRowKey = requestKey;
      void loadRow(requestKey, shown);
    });
  });

  async function loadRow(requestKey, shown) {
    const writesAtStart = overrideWrites;
    try {
      const result = await context.chatController.getSession(
        shown.agentAddress,
        shown.sessionId,
      );
      // A newer read, or an override write meanwhile, decides instead.
      if (requestKey !== requestedRowKey || writesAtStart !== overrideWrites) {
        return;
      }
      const session = result?.session;
      if (!isRecord(session)) {
        return;
      }
      rows = {
        ...rows,
        [shown.key]: {
          workingProjectId: text(session.working_project_id) || null,
          projectKnown: true,
          overrides: isRecord(session.agent_overrides)
            ? session.agent_overrides
            : {},
        },
      };
    } catch {
      // Best effort: the footer keeps what it showed.
    }
  }

  function updateDraft(key, changes) {
    drafts = { ...drafts, [key]: { ...(drafts[key] ?? {}), ...changes } };
  }

  // `changes` maps override fields to a value, or '' to clear it.
  async function writeOverrides(shown, changes) {
    const key = shown.key;
    const request = Object.fromEntries(
      Object.entries(changes).map(([field, value]) => [field, value || null]),
    );
    const writeId = (latestWrite[key] ?? 0) + 1;
    latestWrite[key] = writeId;
    overrideWrites += 1;
    pending = { ...pending, [key]: { ...(pending[key] ?? {}), ...request } };
    try {
      const result = await context.chatController.setSessionAgentOverrides(
        shown.agentAddress,
        shown.sessionId,
        request,
      );
      if (latestWrite[key] !== writeId) {
        return;
      }
      const projectKnown = rows[key]?.projectKnown === true;
      rows = {
        ...rows,
        [key]: {
          workingProjectId: rows[key]?.workingProjectId ?? null,
          projectKnown,
          overrides: isRecord(result?.agent_overrides)
            ? result.agent_overrides
            : {},
        },
      };
      dropPending(key);
      if (!projectKnown && current?.key === key) {
        // The row read this write overlapped was dropped; read it again.
        rowRevision += 1;
      }
    } catch (error) {
      if (latestWrite[key] !== writeId) {
        return;
      }
      dropPending(key);
      context.onError(
        `${t('chat.sessionSettings.saveError')} ${error?.message ?? ''}`.trim(),
        key,
      );
    } finally {
      overrideWrites += 1;
    }
  }

  function dropPending(key) {
    if (!Object.hasOwn(pending, key)) {
      return;
    }
    const next = { ...pending };
    delete next[key];
    pending = next;
  }

  function selectOverrides(changes) {
    const shown = current;
    if (!shown) {
      return;
    }
    if (shown.kind === 'draft') {
      updateDraft(shown.key, changes);
      return;
    }
    void writeOverrides(shown, changes);
  }

  // Whether `modelValue` offers the thinking effort `effort`. Without the
  // catalog nothing is known against it.
  function offersEffort(modelValue, effort) {
    if (!catalog) {
      return true;
    }
    const reasoning = reasoningForModelValue(modelValue, catalog.models);
    return (
      reasoning?.supported !== false &&
      effortOptionsForReasoning(reasoning).includes(effort)
    );
  }

  // An Identity Agent rename keeps its Sessions' and drafts' settings under
  // the new id.
  function renameAgent(oldAgentId, newAgentId) {
    drafts = renameAgentInKeys(drafts, oldAgentId, newAgentId);
    rows = renameAgentInKeys(rows, oldAgentId, newAgentId);
    pending = renameAgentInKeys(pending, oldAgentId, newAgentId);
    for (const key of Object.keys(latestWrite)) {
      const renamed = renameAgentInKey(key, oldAgentId, newAgentId);
      if (renamed !== key) {
        latestWrite[renamed] = latestWrite[key];
        delete latestWrite[key];
      }
    }
  }

  return {
    renameAgent,
    get view() {
      return view;
    },
    loadCatalog,
    // The draft's Project: '' picks the Workspace, else a Project id.
    selectProject(value) {
      const shown = current;
      if (shown?.kind !== 'draft' || view?.project.editable !== true) {
        return;
      }
      updateDraft(shown.key, { workingProjectId: value || null });
    },
    // '' returns to the Agent's Model or thinking effort. A chosen effort
    // the next Model does not offer returns to the Agent's as well.
    selectModel(value) {
      const changes = { model: value };
      const effort = view?.thinkingEffort ?? '';
      if (effort && !offersEffort(value || view.defaultModel, effort)) {
        changes.thinking_effort = '';
      }
      selectOverrides(changes);
    },
    selectThinkingEffort(value) {
      selectOverrides({ thinking_effort: value });
    },
    // Where the displayed conversation's commands, Skills and files come
    // from: its Session, or the Project its draft chose (none: the Agent's
    // default Project).
    readScope() {
      const shown = current;
      if (shown?.kind === 'session') {
        return { sessionId: shown.sessionId };
      }
      const projectId = shown ? drafts[shown.key]?.workingProjectId : undefined;
      return projectId === undefined ? {} : { workingProjectId: projectId };
    },
    // The draft with what its new Session should start with.
    draftWithSettings(draft) {
      const settings = drafts[draft.key] ?? {};
      return {
        ...draft,
        ...(settings.workingProjectId !== undefined
          ? { workingProjectId: settings.workingProjectId }
          : {}),
        agentOverrides: {
          model: settings.model || undefined,
          thinking_effort: settings.thinking_effort || undefined,
        },
      };
    },
    // The created Session starts from the submitted choices until its own
    // row answers. Only consume those choices; later draft edits still belong
    // to the next draft, not to the Session this older submission created.
    draftSent(draft, sessionState) {
      const settings = drafts[draft.key] ?? {};
      const { projectId: teamProjectId } = parseAgentAddress(
        draft.agentAddress,
      );
      rows = {
        ...rows,
        [sessionState.key]: {
          workingProjectId:
            teamProjectId ||
            (draft.workingProjectId !== undefined
              ? draft.workingProjectId
              : agentDefaults(draft.agentAddress).rootProjectId),
          projectKnown: true,
          overrides: Object.fromEntries(
            [
              ['model', draft.agentOverrides?.model],
              ['thinking_effort', draft.agentOverrides?.thinking_effort],
            ].filter(([, value]) => value),
          ),
        },
      };
      if (
        settings.workingProjectId === draft.workingProjectId &&
        (settings.model || undefined) === draft.agentOverrides?.model &&
        (settings.thinking_effort || undefined) ===
          draft.agentOverrides?.thinking_effort
      ) {
        const next = { ...drafts };
        delete next[draft.key];
        drafts = next;
      }
    },
    // `/model <value|reset>` wrote the Agent's Model and cleared the
    // Session's own: a draft drops the Model it held, a Session reads its
    // overrides again, and the Agent's defaults are refreshed.
    modelCommandApplied({ agentAddress, draftKey = '', sessionKey = '' }) {
      if (draftKey && drafts[draftKey]?.model) {
        updateDraft(draftKey, { model: '' });
      }
      if (sessionKey) {
        dropPending(sessionKey);
        if (current?.key === sessionKey) {
          rowRevision += 1;
        }
      }
      context.refreshAgentDefaults(agentAddress);
    },
  };
}

function text(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
