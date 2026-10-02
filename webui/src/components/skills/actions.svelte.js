import { t } from '$lib/i18n.js';
import { createSkillDocument } from './skillsView.js';
import {
  accessPatch,
  projectSkillPatch,
  skillAccessOf,
} from './skillAccess.js';
import {
  createSkill as createSkillRequest,
  updateSkill,
  setSkillDisabled,
  setSkillPinned,
  revertSkillRevisions,
  restoreSkill,
  purgeSkill,
  shareSkill,
  updateAgent,
  setProject,
  deleteSkill as deleteSkillRequest,
} from '$lib/api.js';

// Every write of the Skills manager. Each request is immediate, runs one at a
// time (`busy`), reloads the inventory afterwards and reports failures as
// toasts. Delete, revert and permanent delete ask first. `context` supplies
// the projection agents, the inspected package, `onToast` and
// `loadInventory`.
export function createSkillActions(context) {
  const GLOBAL_SCOPE = 'global';

  let busy = $state(false);

  // Create dialog: a target scope (global pool or an Agent's private home)
  // plus the name/description/instructions draft.
  let showCreateModal = $state(false);
  let createScope = $state(GLOBAL_SCOPE);
  let newName = $state('');
  let newDescription = $state('');
  let newContent = $state('');

  // Edit dialog: which package (scope + name) is open with which content.
  let editing = $state(null);
  let editContent = $state('');

  // The package awaiting delete confirmation (null = dialog closed).
  let deleteTarget = $state(null);

  // The revert awaiting confirmation: `{ scope, name, revisions, later }`;
  // `later` is the revision the server named as changing the same part of
  // the Skill afterwards, which the revert then includes (null at first).
  let revertTarget = $state(null);

  // The archived package awaiting permanent-delete confirmation.
  let purgeTarget = $state(null);

  let createDisabled = $derived(
    busy || !newName.trim() || !newDescription.trim() || !newContent.trim(),
  );

  let scopeOptions = $derived([
    {
      value: GLOBAL_SCOPE,
      label: t('settings.skills.scopeGlobal'),
    },
    ...context.agents.map((agent) => ({
      value: `agent:${agent.id}`,
      label: t('settings.skills.scopeAgent', {
        name: agent.name || agent.id,
      }),
    })),
  ]);

  // `recover(error)` may take over a failure instead of the error toast.
  async function run(request, failure, success = null, recover = null) {
    if (busy) return false;
    busy = true;
    try {
      await request();
      if (success) context.onToast({ title: success(), variant: 'success' });
      await context.loadInventory();
      return true;
    } catch (error) {
      if (recover?.(error)) return false;
      context.onToast({
        title: `${failure()} ${error.message}`,
        variant: 'error',
      });
      return false;
    } finally {
      busy = false;
    }
  }

  function openCreateModal(scope = GLOBAL_SCOPE) {
    createScope = scope;
    newName = '';
    newDescription = '';
    newContent = '';
    showCreateModal = true;
  }

  function closeCreateModal() {
    showCreateModal = false;
    newName = '';
    newDescription = '';
    newContent = '';
  }

  async function createSkill() {
    if (createDisabled) return;
    await run(
      async () => {
        await createSkillRequest({
          scope: createScope,
          name: newName.trim(),
          content: createSkillDocument(newName, newDescription, newContent),
        });
        closeCreateModal();
      },
      () => t('settings.skills.createError'),
      () => t('settings.skills.created'),
    );
  }

  function startEdit(entry) {
    if (busy || !entry.editable_scope || context.inspected?.id !== entry.id)
      return;
    editing = {
      scope: entry.editable_scope,
      name: entry.name,
      shared: entry.shared,
    };
    editContent = context.inspected.content;
  }

  function closeEditModal() {
    editing = null;
    editContent = '';
  }

  async function saveEdit() {
    if (!editing) return;
    const target = editing;
    await run(
      async () => {
        await updateSkill({
          scope: target.scope,
          name: target.name,
          content: editContent,
        });
        closeEditModal();
      },
      () => t('settings.skills.contentSaveError'),
      () => t('settings.skills.saved'),
    );
  }

  // The global off switch for every package with this name.
  function setDisabled(entry, disabled) {
    return run(
      () => setSkillDisabled(entry.name, disabled),
      () => t('skills.toggleError'),
      () =>
        disabled
          ? t('skills.disabledToast', { name: entry.name })
          : t('skills.enabledToast', { name: entry.name }),
    );
  }

  // Saves an Agent's next allowlist pair; only changed fields are sent.
  function updateAgentAccess(agent, next) {
    const patch = accessPatch(skillAccessOf(agent), next);
    if (!Object.keys(patch).length) return Promise.resolve(true);
    return run(
      () => updateAgent({ id: agent.id, ...patch }),
      () => t('skills.accessError'),
    );
  }

  // Replaces the receivers of a private package; none stops sharing.
  function setSharing(entry, receivers) {
    return run(
      () =>
        shareSkill(entry.owner_id, entry.name, receivers.length > 0, receivers),
      () => t('skills.shareError'),
    );
  }

  // Activates or deactivates Skills of one pool in a Project.
  function updateProjectSkills(project, source, names, active) {
    const patch = projectSkillPatch(project, source, names, active);
    if (!Object.keys(patch).length) return Promise.resolve(true);
    return run(
      () => setProject(project.project_id, patch),
      () => t('skills.projectError'),
    );
  }

  // Pins keep background reviews from changing an editable package.
  function setPinned(entry, pinned) {
    if (!entry.editable_scope) return Promise.resolve(false);
    return run(
      () => setSkillPinned(entry.editable_scope, entry.name, pinned),
      () => t('skills.pin.error'),
      () =>
        pinned
          ? t('skills.pin.pinnedToast', { name: entry.name })
          : t('skills.pin.unpinnedToast', { name: entry.name }),
    );
  }

  function requestRevert(entry, revision) {
    if (busy || !entry.editable_scope) return;
    revertTarget = {
      scope: entry.editable_scope,
      name: entry.name,
      revisions: [revision.id],
      later: null,
    };
  }

  function cancelRevert() {
    revertTarget = null;
  }

  // A revision that a later one changed again reverts only together with it:
  // the server names that revision, and the dialog asks to include it.
  function laterRevision(error, revisions) {
    const later = error?.details?.data?.later;
    return error?.code === 'domain_error' &&
      Number.isInteger(later) &&
      !revisions.includes(later)
      ? later
      : null;
  }

  async function confirmRevert() {
    const target = revertTarget;
    revertTarget = null;
    if (!target) return;
    await run(
      () => revertSkillRevisions(target.scope, target.revisions),
      () => t('skills.revert.error'),
      () =>
        target.revisions.length === 1
          ? t('skills.revert.doneOne', { revision: target.revisions[0] })
          : t('skills.revert.doneMany', {
              revisions: target.revisions.join(', '),
            }),
      (error) => {
        const later = laterRevision(error, target.revisions);
        if (later === null) return false;
        revertTarget = {
          ...target,
          revisions: [...target.revisions, later].sort((a, b) => a - b),
          later,
        };
        return true;
      },
    );
  }

  // Puts an archived package back under its name in its scope.
  function restoreArchived(item) {
    return run(
      () => restoreSkill(item.scope, item.archive_id),
      () => t('skills.archived.restoreError'),
      () => t('skills.archived.restored', { name: item.name }),
    );
  }

  function requestPurge(item) {
    if (busy) return;
    purgeTarget = {
      scope: item.scope,
      archiveId: item.archive_id,
      name: item.name,
    };
  }

  function cancelPurge() {
    purgeTarget = null;
  }

  async function confirmPurge() {
    const target = purgeTarget;
    purgeTarget = null;
    if (!target) return;
    await run(
      () => purgeSkill(target.scope, target.archiveId),
      () => t('skills.archived.purgeError'),
      () => t('skills.archived.purged', { name: target.name }),
    );
  }

  function requestDelete(entry) {
    if (busy || !entry.editable_scope) return;
    deleteTarget = { scope: entry.editable_scope, name: entry.name };
  }

  function cancelDelete() {
    deleteTarget = null;
  }

  async function confirmDelete() {
    const target = deleteTarget;
    deleteTarget = null;
    if (!target) return;
    await run(
      async () => {
        await deleteSkillRequest(target.scope, target.name);
        if (editing?.name === target.name && editing?.scope === target.scope)
          closeEditModal();
      },
      () => t('settings.skills.deleteError'),
      () => t('settings.skills.deleted'),
    );
  }

  return {
    GLOBAL_SCOPE,
    get busy() {
      return busy;
    },
    get showCreateModal() {
      return showCreateModal;
    },
    get createScope() {
      return createScope;
    },
    set createScope(value) {
      createScope = value;
    },
    get newName() {
      return newName;
    },
    set newName(value) {
      newName = value;
    },
    get newDescription() {
      return newDescription;
    },
    set newDescription(value) {
      newDescription = value;
    },
    get newContent() {
      return newContent;
    },
    set newContent(value) {
      newContent = value;
    },
    get editing() {
      return editing;
    },
    get editContent() {
      return editContent;
    },
    set editContent(value) {
      editContent = value;
    },
    get deleteTarget() {
      return deleteTarget;
    },
    get revertTarget() {
      return revertTarget;
    },
    get purgeTarget() {
      return purgeTarget;
    },
    get createDisabled() {
      return createDisabled;
    },
    get scopeOptions() {
      return scopeOptions;
    },
    openCreateModal,
    closeCreateModal,
    createSkill,
    startEdit,
    closeEditModal,
    saveEdit,
    setDisabled,
    updateAgentAccess,
    setSharing,
    updateProjectSkills,
    requestDelete,
    cancelDelete,
    confirmDelete,
    setPinned,
    requestRevert,
    cancelRevert,
    confirmRevert,
    restoreArchived,
    requestPurge,
    cancelPurge,
    confirmPurge,
  };
}
