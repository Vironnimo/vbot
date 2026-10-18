import { t } from '../../../../webui/src/lib/i18n.js';
import { tick } from 'svelte';
import { canStop, requestId } from './pagePresentation.js';
import {
  deliveryChanges,
  deliveryLimitError,
  normalizeDelivery,
  runTitle,
  settingsRunStates,
  workingRunStates,
} from './runPresentation.js';
import { failureText } from './failureText.js';

export function createSwarmRunActions(context) {
  let profileDeleteCandidate = $state(null);

  let swarmDeleteCandidate = $state(null);

  let deleteError = $state('');

  let settingsRun = $state(null);

  let deliveryDraft = $state(null);

  let settingsError = $state('');

  let detailsRun = $state(null);

  let dialogRequest = 0;

  const deliveryCheck = $derived(
    deliveryDraft ? normalizeDelivery(deliveryDraft) : null,
  );

  const deliveryErrors = $derived(
    Object.fromEntries(
      (deliveryCheck?.invalid ?? []).map((limit) => [
        limit.field,
        deliveryLimitError(limit),
      ]),
    ),
  );

  const deliveryChangeLines = $derived(
    settingsRun && deliveryCheck
      ? deliveryChanges(
          settingsRun.delivery,
          deliveryCheck.value,
          deliveryCheck.invalid,
        )
      : [],
  );

  function confirmProfileDelete(profile) {
    context.error = '';
    deleteError = '';
    profileDeleteCandidate = profile;
  }

  function confirmRunDelete(run) {
    context.error = '';
    deleteError = '';
    swarmDeleteCandidate = run;
  }

  function cancelDelete() {
    if (context.pending === 'delete') return;
    profileDeleteCandidate = null;
    swarmDeleteCandidate = null;
  }

  function paneNeighbor(kind, id) {
    const key = `${kind}Id`;
    const rows = [...document.querySelectorAll(`[data-${kind}-id]`)];
    const index = rows.findIndex((row) => row.dataset[key] === id);
    if (index < 0) return null;
    return (rows[index + 1] ?? rows[index - 1])?.dataset[key] ?? null;
  }

  async function focusPaneRow(kind, id) {
    await tick();
    if (context.disposed) return;
    const row = [...document.querySelectorAll(`[data-${kind}-id]`)].find(
      (item) => item.dataset[`${kind}Id`] === id,
    );
    const target = row ?? document.querySelector('.swarm-new-run');
    (target?.checkVisibility?.() === false
      ? document.getElementById('swarm-picker')
      : target
    )?.focus();
  }

  async function deleteProfile() {
    const candidate = profileDeleteCandidate;
    if (!candidate || context.pending) return;
    context.pending = 'delete';
    deleteError = '';
    const next = paneNeighbor('profile', candidate.id);
    // An autosave may have advanced the revision while the dialog was open.
    const current =
      context.profiles.find((item) => item.id === candidate.id) ?? candidate;
    try {
      await context.call('profiles.delete', {
        profile_id: candidate.id,
        expected_revision: current.revision,
      });
      if (context.editor?.id === candidate.id) {
        // Close without the navigation flush: the draft belongs to the
        // deleted Swarm and must not be saved again.
        context.selectionRequest += 1;
        context.editor = null;
        context.syncRoute({ step: false });
      }
      if (context.selectedProfile?.id === candidate.id)
        void context.selectRunProfile(null);
      profileDeleteCandidate = null;
      await context.refresh();
      await focusPaneRow('profile', next);
    } catch (cause) {
      deleteError = failureText(cause);
    } finally {
      context.pending = '';
    }
  }

  async function deleteSwarm() {
    const candidate = swarmDeleteCandidate;
    if (!candidate || context.pending) return;
    context.pending = 'delete';
    deleteError = '';
    const next = paneNeighbor('run', candidate.id);
    try {
      // An open Run whose participants are all idle offers Resume, not Stop;
      // deletion closes it first because the Store deletes only stopped Runs.
      // A retry after a failed deletion starts from the stopped state.
      if (canStop(candidate.state)) {
        const stopped = await context.call('swarms.stop', {
          swarm_id: candidate.id,
          request_id: requestId(),
        });
        swarmDeleteCandidate = { ...candidate, state: stopped.state };
      }
      await context.call('swarms.delete', { swarm_id: candidate.id });
      if (context.selectedSwarm?.id === candidate.id) {
        context.showStart();
        context.syncRoute({ step: false });
      }
      swarmDeleteCandidate = null;
      await context.refresh();
      await focusPaneRow('run', next);
    } catch (cause) {
      deleteError = failureText(cause);
    } finally {
      context.pending = '';
    }
  }

  function runMenu(run) {
    const busy =
      run.id === context.selectedSwarm?.id
        ? context.working
        : workingRunStates.includes(run.state);
    const open = settingsRunStates.includes(run.state);
    return {
      label: t('swarm.run.actions', { title: runTitle(run) }),
      items: [
        {
          id: 'communication',
          label: t('swarm.run.communication'),
          group: 'run',
          disabled: !open,
          hint: open ? undefined : t('swarm.run.settingsClosed'),
          onSelect: () => void openRunDialog('settings', run),
        },
        {
          id: 'details',
          label: t('swarm.run.details'),
          group: 'run',
          onSelect: () => void openRunDialog('details', run),
        },
        {
          id: 'delete',
          label: t('swarm.menu.delete'),
          danger: true,
          group: 'delete',
          disabled: busy || Boolean(context.pending),
          hint: busy ? t('swarm.deleteRun.stopFirst') : undefined,
          onSelect: () => confirmRunDelete(run),
        },
      ],
    };
  }

  function profileMenu(profile) {
    return {
      label: t('swarm.sidebar.profileActions', { name: profile.name }),
      items: [
        {
          id: 'edit',
          label: t('common.edit'),
          group: 'profile',
          onSelect: () =>
            void context.navigate(() => context.openProfile(profile)),
        },
        {
          id: 'start',
          label: t('swarm.sidebar.startRun'),
          group: 'profile',
          onSelect: () => void context.startRunWith(profile),
        },
        {
          id: 'delete',
          label: t('swarm.menu.delete'),
          danger: true,
          group: 'delete',
          disabled: Boolean(context.pending),
          onSelect: () => confirmProfileDelete(profile),
        },
      ],
    };
  }

  async function openRunDialog(kind, run) {
    const request = ++dialogRequest;
    context.error = '';
    let detail =
      context.selectedSwarm?.id === run.id ? context.selectedSwarm : null;
    if (!detail) {
      try {
        detail = (await context.call('swarms.get', { swarm_id: run.id })).swarm;
      } catch (cause) {
        if (!context.disposed && request === dialogRequest)
          context.error = failureText(cause);
        return;
      }
      if (context.disposed || request !== dialogRequest) return;
    }
    const record = {
      ...context.swarms.find((item) => item.id === run.id),
      ...detail,
    };
    if (kind === 'settings') openDelivery(record);
    else detailsRun = record;
  }

  function openDelivery(run) {
    if (!run?.delivery) return;
    deliveryDraft = JSON.parse(JSON.stringify(run.delivery));
    settingsError = '';
    settingsRun = run;
  }

  function changeDelivery(next) {
    deliveryDraft = next;
  }

  function closeSettings() {
    if (context.pending === 'settings') return;
    settingsRun = null;
    deliveryDraft = null;
    settingsError = '';
  }

  async function applyDelivery() {
    const run = settingsRun;
    const check = deliveryCheck;
    if (!run || !check || check.invalid.length || context.pending) return;
    context.pending = 'settings';
    settingsError = '';
    try {
      await context.call('swarms.settings', {
        swarm_id: run.id,
        delivery: JSON.parse(JSON.stringify(check.value)),
        expected_revision: run.settings_revision,
        request_id: requestId(),
      });
    } catch (cause) {
      if (!context.disposed) settingsError = failureText(cause);
      return;
    } finally {
      context.pending = '';
    }
    if (context.disposed) return;
    closeSettings();
    if (context.selectedSwarm?.id !== run.id) return;
    try {
      const swarm = (await context.call('swarms.get', { swarm_id: run.id }))
        .swarm;
      if (!context.disposed && context.selectedSwarm?.id === run.id)
        context.selectedSwarm = swarm;
    } catch (cause) {
      if (context.selectedSwarm?.id === run.id) context.refreshFailed(cause);
    }
  }

  return {
    closeDialogs() {
      dialogRequest += 1;
      detailsRun = null;
      closeSettings();
      cancelDelete();
    },
    closeDetails() {
      detailsRun = null;
    },
    get profileDeleteCandidate() {
      return profileDeleteCandidate;
    },
    get swarmDeleteCandidate() {
      return swarmDeleteCandidate;
    },
    get deleteError() {
      return deleteError;
    },
    get settingsRun() {
      return settingsRun;
    },
    get deliveryDraft() {
      return deliveryDraft;
    },
    get settingsError() {
      return settingsError;
    },
    get detailsRun() {
      return detailsRun;
    },
    get deliveryErrors() {
      return deliveryErrors;
    },
    get deliveryChangeLines() {
      return deliveryChangeLines;
    },
    confirmProfileDelete,
    confirmRunDelete,
    cancelDelete,
    deleteProfile,
    deleteSwarm,
    runMenu,
    profileMenu,
    openRunDialog,
    changeDelivery,
    closeSettings,
    applyDelivery,
  };
}
