import { t } from '$lib/i18n.js';
import { createSkillDocument } from './skillsView.js';
import {
  createSkill as createSkillRequest,
  updateSkill,
  setSkillDisabled,
  shareSkill,
  deleteSkill as deleteSkillRequest,
} from '$lib/api.js';

export function createSkillActions(context) {
  const GLOBAL_SCOPE = 'global';

  let busy = $state(false);

  // Create-modal state: a target scope (global pool or an agent's private home)
  // plus the name/content draft.
  let showCreateModal = $state(false);

  let createScope = $state(GLOBAL_SCOPE);

  let newName = $state('');

  let newDescription = $state('');

  let newContent = $state('');

  // Edit-modal state: which entry (scope + name) is open with which content.
  let editing = $state(null);

  // { scope, name }
  let editContent = $state('');

  // The skill awaiting delete confirmation (null = dialog closed).
  let deleteTarget = $state(null);

  // { scope, name }
  // Share-modal state: which entry is being shared and which receivers are
  // selected.
  let shareTarget = $state(null);

  // { owner_id, name }
  let shareReceivers = $state([]);

  let createDisabled = $derived(
    busy || !newName.trim() || !newDescription.trim() || !newContent.trim(),
  );

  let scopeOptions = $derived([
    {
      value: GLOBAL_SCOPE,
      label: t('settings.skills.scopeGlobal', 'Global skills'),
    },
    ...context.agents.map((agent) => ({
      value: `agent:${agent.id}`,
      label: t('settings.skills.scopeAgent', '{name} (private)', {
        name: agent.name || agent.id,
      }),
    })),
  ]);

  function openCreateModal() {
    if (context.scope !== 'directories')
      createScope = context.scope.startsWith('agent:')
        ? context.scope
        : GLOBAL_SCOPE;
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
    if (createDisabled) {
      return;
    }
    busy = true;
    try {
      await createSkillRequest({
        scope: createScope,
        name: newName.trim(),
        content: createSkillDocument(newName, newDescription, newContent),
      });
      context.onToast({
        title: t('settings.skills.created', 'Skill created.'),
        variant: 'success',
      });
      closeCreateModal();
      await context.loadInventory();
    } catch (error) {
      context.onToast({
        title: `${t('settings.skills.createError', 'Skill could not be created.')} ${error.message}`,
        variant: 'error',
      });
    } finally {
      busy = false;
    }
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
    if (busy || !editing) {
      return;
    }
    busy = true;
    try {
      await updateSkill({
        scope: editing.scope,
        name: editing.name,
        content: editContent,
      });
      context.onToast({
        title: t('settings.skills.saved', 'Skill saved.'),
        variant: 'success',
      });
      closeEditModal();
      await context.loadInventory();
    } catch (error) {
      context.onToast({
        title: `${t('settings.skills.contentSaveError', 'Skill could not be saved.')} ${error.message}`,
        variant: 'error',
      });
    } finally {
      busy = false;
    }
  }

  async function toggleDisabled(entry) {
    if (busy) {
      return;
    }
    busy = true;
    try {
      await setSkillDisabled(entry.name, !entry.disabled);
      context.onToast({
        title: entry.disabled
          ? t('skills.enabledToast', 'Skill "{name}" enabled.', {
              name: entry.name,
            })
          : t('skills.disabledToast', 'Skill "{name}" disabled everywhere.', {
              name: entry.name,
            }),
        variant: 'success',
      });
      await context.loadInventory();
    } catch (error) {
      context.onToast({
        title: `${t('skills.toggleError', 'The skill could not be changed.')} ${error.message}`,
        variant: 'error',
      });
    } finally {
      busy = false;
    }
  }

  function openShareModal(entry) {
    if (busy || !entry.owner_id) {
      return;
    }
    shareTarget = { owner_id: entry.owner_id, name: entry.name };
    shareReceivers = Array.isArray(entry.shared_with)
      ? [...entry.shared_with]
      : [];
  }

  function closeShareModal() {
    shareTarget = null;
    shareReceivers = [];
  }

  function toggleReceiver(agentId) {
    if (shareReceivers.includes(agentId)) {
      shareReceivers = shareReceivers.filter((id) => id !== agentId);
    } else {
      shareReceivers = [...shareReceivers, agentId];
    }
  }

  let shareableAgents = $derived(
    context.agents.filter((agent) => agent.id !== shareTarget?.owner_id),
  );

  let shareSaveDisabled = $derived(busy || Boolean(context.agentError));

  async function saveShare() {
    if (busy || !shareTarget || context.agentError) {
      return;
    }
    busy = true;
    try {
      await shareSkill(
        shareTarget.owner_id,
        shareTarget.name,
        shareReceivers.length > 0,
        shareReceivers,
      );
      context.onToast({
        title: t('skills.sharedToast', 'Skill shared with {count} agents.', {
          count: shareReceivers.length,
        }),
        variant: 'success',
      });
      closeShareModal();
      await context.loadInventory();
    } catch (error) {
      context.onToast({
        title: `${t('skills.shareError', 'Sharing could not be changed.')} ${error.message}`,
        variant: 'error',
      });
    } finally {
      busy = false;
    }
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
    if (!target || busy) {
      return;
    }
    busy = true;
    try {
      await deleteSkillRequest(target.scope, target.name);
      context.onToast({
        title: t('settings.skills.deleted', 'Skill deleted.'),
        variant: 'success',
      });
      if (editing?.name === target.name && editing?.scope === target.scope) {
        closeEditModal();
      }
      await context.loadInventory();
    } catch (error) {
      context.onToast({
        title: `${t('settings.skills.deleteError', 'Skill could not be deleted.')} ${error.message}`,
        variant: 'error',
      });
    } finally {
      busy = false;
    }
  }
  return {
    get GLOBAL_SCOPE() {
      return GLOBAL_SCOPE;
    },
    get busy() {
      return busy;
    },
    set busy(value) {
      busy = value;
    },
    get showCreateModal() {
      return showCreateModal;
    },
    set showCreateModal(value) {
      showCreateModal = value;
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
    set editing(value) {
      editing = value;
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
    set deleteTarget(value) {
      deleteTarget = value;
    },
    get shareTarget() {
      return shareTarget;
    },
    set shareTarget(value) {
      shareTarget = value;
    },
    get shareReceivers() {
      return shareReceivers;
    },
    set shareReceivers(value) {
      shareReceivers = value;
    },
    get createDisabled() {
      return createDisabled;
    },
    set createDisabled(value) {
      createDisabled = value;
    },
    get scopeOptions() {
      return scopeOptions;
    },
    set scopeOptions(value) {
      scopeOptions = value;
    },
    get openCreateModal() {
      return openCreateModal;
    },
    get closeCreateModal() {
      return closeCreateModal;
    },
    get createSkill() {
      return createSkill;
    },
    get startEdit() {
      return startEdit;
    },
    get closeEditModal() {
      return closeEditModal;
    },
    get saveEdit() {
      return saveEdit;
    },
    get toggleDisabled() {
      return toggleDisabled;
    },
    get openShareModal() {
      return openShareModal;
    },
    get closeShareModal() {
      return closeShareModal;
    },
    get toggleReceiver() {
      return toggleReceiver;
    },
    get shareableAgents() {
      return shareableAgents;
    },
    set shareableAgents(value) {
      shareableAgents = value;
    },
    get shareSaveDisabled() {
      return shareSaveDisabled;
    },
    set shareSaveDisabled(value) {
      shareSaveDisabled = value;
    },
    get saveShare() {
      return saveShare;
    },
    get requestDelete() {
      return requestDelete;
    },
    get cancelDelete() {
      return cancelDelete;
    },
    get confirmDelete() {
      return confirmDelete;
    },
  };
}
