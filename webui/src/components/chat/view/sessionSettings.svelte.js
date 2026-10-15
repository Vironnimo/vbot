import { untrack } from 'svelte';

import { effortForReasoning, reasoningForModelValue } from '$lib/agentForm.js';
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
  // Each Session owns its write revision and field-level reconciliation. Reads
  // cannot establish ordering while a write runs; the final completion rereads
  // the row. Independent fields keep their own pending and successful owners.
  const overrideWrites = {};

  function writesFor(key) {
    return (overrideWrites[key] ??= {
      key,
      revision: 0,
      nextId: 0,
      active: 0,
      owners: {},
      committed: {},
    });
  }

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
    const writes = writesFor(shown.key);
    const revisionAtStart = writes.revision;
    const writingAtStart = writes.active > 0;
    try {
      const result = await context.chatController.getSession(
        shown.agentAddress,
        shown.sessionId,
      );
      // A read overlapping a write may predate its commit even if the RPC
      // reply has not arrived yet. The last write schedules a replacement.
      if (
        requestKey !== requestedRowKey ||
        writingAtStart ||
        revisionAtStart !== writes.revision
      ) {
        return;
      }
      const session = result?.session;
      if (!isRecord(session)) {
        return;
      }
      context.chatController.applySessionCompactionPolicy(
        shown.agentAddress,
        shown.sessionId,
        session.compaction_policy_effective,
      );
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
    const writes = writesFor(key);
    const request = Object.fromEntries(
      Object.entries(changes).map(([field, value]) => [field, value || null]),
    );
    const writeId = ++writes.nextId;
    writes.revision += 1;
    writes.active += 1;
    for (const field of Object.keys(request)) {
      writes.owners[field] = writeId;
    }
    pending = { ...pending, [key]: { ...(pending[key] ?? {}), ...request } };
    try {
      const result = await context.chatController.setSessionAgentOverrides(
        shown.agentAddress,
        shown.sessionId,
        request,
      );
      const row = rows[writes.key];
      const overrides = { ...(row?.overrides ?? {}) };
      const saved = isRecord(result?.agent_overrides)
        ? result.agent_overrides
        : {};
      // A full reply may contain an obsolete snapshot of other fields. Keep
      // evidence only for the fields this request wrote, without replacing a
      // newer success or hiding a newer optimistic choice on the same field.
      for (const field of Object.keys(request)) {
        if ((writes.committed[field] ?? 0) > writeId) {
          continue;
        }
        writes.committed[field] = writeId;
        if (Object.hasOwn(saved, field)) {
          overrides[field] = saved[field];
        } else {
          delete overrides[field];
        }
      }
      rows = {
        ...rows,
        [writes.key]: {
          workingProjectId: row?.workingProjectId ?? null,
          projectKnown: row?.projectKnown === true,
          overrides,
        },
      };
      dropPending(writes.key, writeId);
    } catch (error) {
      if (!dropPending(writes.key, writeId)) {
        return;
      }
      context.onError(
        `${t('chat.sessionSettings.saveError')} ${error?.message ?? ''}`.trim(),
        writes.key,
      );
    } finally {
      writes.revision += 1;
      writes.active -= 1;
      if (writes.active === 0 && current?.key === writes.key) {
        // Responses have no server revision, so concurrent same-field commits
        // and invalidation reads need a snapshot taken after all writes settle.
        // Also recover a dropped initial read after either success or failure.
        rowRevision += 1;
      }
    }
  }

  function dropPending(key, writeId) {
    const writes = writesFor(key);
    const changes = { ...(pending[key] ?? {}) };
    let dropped = false;
    for (const field of Object.keys(changes)) {
      if (writeId === undefined || writes.owners[field] === writeId) {
        delete changes[field];
        delete writes.owners[field];
        dropped = true;
      }
    }
    const next = { ...pending };
    if (Object.keys(changes).length > 0) {
      next[key] = changes;
    } else {
      delete next[key];
    }
    pending = next;
    return dropped;
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

  // An Identity Agent rename keeps its Sessions' and drafts' settings under
  // the new id.
  function renameAgent(oldAgentId, newAgentId) {
    drafts = renameAgentInKeys(drafts, oldAgentId, newAgentId);
    rows = renameAgentInKeys(rows, oldAgentId, newAgentId);
    pending = renameAgentInKeys(pending, oldAgentId, newAgentId);
    for (const key of Object.keys(overrideWrites)) {
      const renamed = renameAgentInKey(key, oldAgentId, newAgentId);
      if (renamed !== key) {
        const writes = overrideWrites[key];
        writes.key = renamed;
        overrideWrites[renamed] = writes;
        delete overrideWrites[key];
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
    // '' returns to the Agent's Model. Keep an explicit effort on the nearest
    // level the next Model offers, saving both choices in the same write.
    selectModel(value) {
      const changes = { model: value };
      const effort = view?.thinkingEffort ?? '';
      const nextEffort = effortForReasoning(
        effort,
        reasoningForModelValue(value || view?.defaultModel, catalog?.models),
      );
      if (nextEffort !== effort) {
        changes.thinking_effort = nextEffort;
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
