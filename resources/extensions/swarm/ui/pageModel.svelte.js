import { t, init } from '../../../../webui/src/lib/i18n.js';
import {
  canStop,
  participantState,
  resumableParticipantState,
  page,
  requestId,
} from './pagePresentation.js';
import {
  deliveryChanges,
  deliveryLimitError,
  normalizeDelivery,
  openRunStates,
  runTitle,
  settingsRunStates,
} from './runPresentation.js';
import { setApplicationTimeZone } from '../../../../webui/src/lib/dateTimePrefs.svelte.js';
import { onMount, tick } from 'svelte';
import { createExtensionPageClient } from '$lib/extensionPageClient.js';
import { sameServerPath } from '$lib/pathPicker.js';
import {
  createPageRefresh,
  everythingChanged,
  swarmChanged,
} from './pageRefresh.js';
import { createSwarmPageActivity } from './pageActivity.svelte.js';
import { createSwarmPageBoard } from './pageBoard.svelte.js';
import { createSwarmPageUsage } from './pageUsage.svelte.js';
import { failureText } from './failureText.js';

// How often relative times ("started 5 minutes ago") move on.
const CLOCK_INTERVAL_MS = 60_000;

// A Run in these states still works, so it cannot be deleted yet; the shown
// Run also counts its running participants (`working`).
const WORKING_RUN_STATES = ['preparing', 'running', 'stopping'];

// The start form's fields in order, with the control each one focuses.
const START_FIELDS = [
  ['profile', 'swarm-start-profile'],
  ['directory', 'swarm-start-directory'],
  ['goal', 'swarm-goal'],
];

// `list` followed by the entries of `more` it does not hold yet.
function appendNew(list, more) {
  const known = new Set(list.map((item) => item.id));
  return [...list, ...more.filter((item) => !known.has(item.id))];
}

// The page's state across its areas: the profile and Run lists, the shown
// Swarm or profile with its route and tab, the start form, Run lifecycle and
// deletion, the Run dialogs, refreshes and the bridge lifetime. The Board,
// Activity and Usage areas keep their own state and requests in `board`,
// `activity` and `usage`.
export function createSwarmPageModel(host) {
  // Exists from the start, so the page root can hand its layer registry to
  // the page's dialogs before any of them mounts.
  const client = host.bridgeClient ?? createExtensionPageClient();

  const call = (operation, arguments_ = {}) =>
    client.operation(operation, arguments_);

  let loading = $state(true);

  // A failed user action. The next user action clears it when it starts;
  // refreshes never do, so a background refresh cannot hide it unseen.
  let error = $state('');

  // A failed refresh, a failed read the page started on its own
  // (`refreshFailed`), or the host not answering at all. A refresh that
  // succeeds clears it unless something failed after that refresh started.
  let refreshError = $state('');

  // Counts the failures shown as `refreshError`, so a refresh clears only
  // those from before it started.
  let refreshFailures = 0;

  // Whether the host has initialized the page.
  let connected = $state(false);

  // The moment relative times are measured from; it moves every minute.
  let now = $state(Date.now());

  let profiles = $state([]);

  let swarms = $state([]);

  let catalog = $state({});

  let selectedProfile = $state(null);

  let selectedSwarm = $state(null);

  let profilesCursor = $state(null);

  let swarmsCursor = $state(null);

  let moreProfilesLoading = $state(false);

  let moreSwarmsLoading = $state(false);

  let activeTab = $state('board');

  let editor = $state(null);

  let profileDeleteCandidate = $state(null);

  let swarmDeleteCandidate = $state(null);

  let deleteError = $state('');

  // The Run whose communication settings are open, the draft as the settings
  // fields hand it back (batching limits as typed), and the dialog's error.
  let settingsRun = $state(null);

  let deliveryDraft = $state(null);

  let settingsError = $state('');

  // The Run whose details are open.
  let detailsRun = $state(null);

  let goal = $state('');

  let runDirectory = $state('');

  let defaultDirectory = $state('');

  let directoryLoading = $state(false);

  // The start form's field errors: `profile`, `directory` and `goal`.
  let startErrors = $state({});

  let pending = $state('');

  // Where focus goes once a view shows: `{ kind: 'goal' }` for the start
  // form's goal, `{ kind: 'run', id }` for a Run's header.
  let focusRequest = $state(null);

  let profileEditor = $state(null);

  // A new Swarm's changed draft, kept while its editor is closed
  // (ProfileEditor's `newDraft`); the page's state ends with the page.
  let newProfileDraft = $state.raw(null);

  let wikiPanel = $state(null);

  let editorKey = $state(0);

  let selectionRequest = 0;

  let overviewRequest = 0;

  let changesRequest = 0;

  let profilesRequest = 0;

  let swarmsRequest = 0;

  let directoryRequest = 0;

  // Whether the start form's default directory failed to load, so a refresh
  // loads it again.
  let directoryFailed = false;

  let dialogRequest = 0;

  let disposed = false;

  // The page's place in the app's Back/Forward history is its route: '' for
  // the start page (the goal form), `/swarms/<id>` for a Swarm's Board,
  // `/swarms/<id>/<tab>` for its other tabs and `/profiles/<id>` or
  // `/profiles/new` for the profile editor. `route` is the one the host shows;
  // `applyRoute` shows its target. Opening a Swarm, a profile or the start
  // page pushes the target route and lets the host send it back; a tab
  // switches at once and pushes the route it now shows. A correction (a
  // deleted record, a saved new profile, a route the page cannot show)
  // replaces the route with what the page shows.
  let route = '';

  let routeRequest = 0;

  // The newest route whose target is shown; until then corrections wait,
  // since the target may still be loading.
  let routeSettled = 0;

  // A profile route that arrived before the profiles waits for them.
  let routeWaits = false;

  let profilesLoaded = false;

  const board = createSwarmPageBoard({
    call,
    client,
    get selectedSwarm() {
      return selectedSwarm;
    },
    get selectionRequest() {
      return selectionRequest;
    },
    get disposed() {
      return disposed;
    },
    get error() {
      return error;
    },
    set error(value) {
      error = value;
    },
    navigate,
    stepToBoard() {
      return openTab('board');
    },
    // A Wiki link from the Board; a page that cannot open shows why.
    async openWikiPage(id) {
      try {
        if (!(await openTab('wiki'))) return;
        await tick();
        await wikiPanel?.openPage(id);
      } catch (cause) {
        if (!disposed) error = failureText(cause);
      }
    },
    // A mention shows the participant's Activity, as its chip does.
    inspectParticipant: (participant) =>
      activity.inspectParticipant(participant),
    refresh,
    showResumeFailures,
  });

  const usage = createSwarmPageUsage({
    call,
    get selectedSwarm() {
      return selectedSwarm;
    },
    get activeTab() {
      return activeTab;
    },
    get disposed() {
      return disposed;
    },
    refreshFailed,
  });

  const backgroundRefresh = createPageRefresh(refreshChanges);

  function navigate(action) {
    if (profileEditor) return profileEditor.requestTransition(action);
    if (wikiPanel) return wikiPanel.requestTransition(action);
    return action();
  }

  const swarmRoute = (id, tab = 'board') =>
    tab === 'board' ? `/swarms/${id}` : `/swarms/${id}/${tab}`;

  function shownRoute() {
    if (editor) return `/profiles/${editor === 'new' ? 'new' : editor.id}`;
    return selectedSwarm ? swarmRoute(selectedSwarm.id, activeTab) : '';
  }

  // What a route names, or null for the start page (also for a route the
  // page does not know, which the correction then replaces).
  function routeTarget(value) {
    const [kind, id, tab, ...rest] = value.split('/').filter(Boolean);
    if (kind === 'swarms' && id && !rest.length) {
      const shownTab = tab ?? 'board';
      if (tabs.some((item) => item.id === shownTab))
        return { swarm: id, tab: shownTab };
    }
    if (kind === 'profiles' && id && tab === undefined) return { profile: id };
    return null;
  }

  // A user step to another place: the host records it and sends the route
  // back, which shows the place.
  function stepTo(next) {
    return client.pushRoute(next).catch((cause) => {
      if (!disposed) error = failureText(cause);
    });
  }

  // Shown content -> route: records it as a user step or corrects the
  // current entry to it.
  function syncRoute({ step }) {
    const shown = shownRoute();
    if (shown === route) return;
    if (!step && routeSettled !== routeRequest) return;
    void (step ? client.pushRoute(shown) : client.replaceRoute(shown)).catch(
      (cause) => {
        if (!disposed) error = failureText(cause);
      },
    );
  }

  // Host route -> shown content. A target the page cannot show leaves the
  // shown content in place and corrects the route to it.
  async function applyRoute(next) {
    route = next;
    // A Swarm or profile still loading for an earlier route stays closed.
    if (routeSettled !== routeRequest) selectionRequest += 1;
    const request = ++routeRequest;
    routeWaits = false;
    const shown = await showRoute(routeTarget(next));
    if (disposed || request !== routeRequest) return;
    if (!shown) {
      routeWaits = true;
      return;
    }
    routeSettled = request;
    syncRoute({ step: false });
  }

  // Returns false while the target waits for the profiles to load.
  async function showRoute(target) {
    if (target?.swarm) {
      if (!editor && selectedSwarm?.id === target.swarm) {
        showTab(target.tab);
        return true;
      }
      closeDialogs();
      await selectSwarm(target.swarm, { tab: target.tab });
      return true;
    }
    if (target?.profile) {
      let profile =
        target.profile === 'new'
          ? 'new'
          : profiles.find((item) => item.id === target.profile);
      if (!profile) {
        if (!profilesLoaded) return false;
        try {
          profile = (await call('profiles.get', { profile_id: target.profile }))
            .profile;
        } catch (cause) {
          if (!disposed) error = failureText(cause);
          return true;
        }
        if (disposed || routeTarget(route)?.profile !== target.profile)
          return true;
        if (!profile) return true;
      }
      // By id: the editor and the list hold separate state proxies of a
      // saved profile.
      if (
        profile === 'new'
          ? editor === 'new'
          : Boolean(editor?.id) && editor.id === profile.id
      )
        return true;
      closeDialogs();
      await showProfile(profile);
      return true;
    }
    if (editor || selectedSwarm) {
      closeDialogs();
      showStart();
    }
    return true;
  }

  // Showing another place closes the dialogs, except one whose action is
  // still running.
  function closeDialogs() {
    board.composeOpen = false;
    dialogRequest += 1;
    detailsRun = null;
    closeSettings();
    cancelDelete();
  }

  // A new visit to the start page keeps the goal but no earlier field errors.
  function showStart() {
    selectionRequest += 1;
    editor = null;
    selectedSwarm = null;
    startErrors = {};
    activity.leaveActivity();
    void selectRunProfile(selectedProfile);
  }

  // The start page, the pane's New Run entry.
  function openStart() {
    return navigate(() => stepTo(''));
  }

  function openSwarm(id) {
    return stepTo(swarmRoute(id, activeTab));
  }

  function showTab(tab) {
    if (tab === activeTab) return;
    activeTab = tab;
    error = '';
    void loadVisibleTab().catch((cause) => {
      if (!disposed) error = failureText(cause);
    });
  }

  async function openTab(tab) {
    if (!selectedSwarm) return false;
    if (tab !== activeTab) await stepTo(swarmRoute(selectedSwarm.id, tab));
    return activeTab === tab;
  }

  function clearStartErrors(...fields) {
    if (fields.some((field) => startErrors[field]))
      startErrors = Object.fromEntries(
        Object.entries(startErrors).filter(
          ([field]) => !fields.includes(field),
        ),
      );
  }

  // Chooses the start form's Swarm and loads its default directory, for a
  // Project from the catalog. `background` marks a choice a refresh made, whose
  // failure is a refresh failure.
  async function selectRunProfile(profile, { background = false } = {}) {
    const request = ++directoryRequest;
    selectedProfile = profile;
    runDirectory = '';
    defaultDirectory = '';
    directoryLoading = false;
    directoryFailed = false;
    clearStartErrors('profile', 'directory');
    if (!profile) return;
    const directory = profile.working_directory;
    if (directory.kind === 'directory') {
      runDirectory = defaultDirectory = directory.path;
      return;
    }
    directoryLoading = true;
    try {
      const nextCatalog = (await call('catalog')).catalog;
      if (disposed || request !== directoryRequest) return;
      const project = nextCatalog.projects?.find(
        (item) => item.id === directory.project_id,
      );
      runDirectory = defaultDirectory = project?.cwd ?? '';
    } catch (cause) {
      if (disposed || request !== directoryRequest) return;
      directoryFailed = true;
      if (background) refreshFailed(cause);
      else error = failureText(cause);
    } finally {
      if (!disposed && request === directoryRequest) directoryLoading = false;
    }
  }

  // Open Runs (working, waiting for messages or needing the user) come
  // first, so a Run that needs attention never hides among finished ones.
  const runGroups = $derived([
    {
      id: 'active',
      label: t('swarm.runs.active'),
      empty: t('swarm.runs.noActive'),
      entries: swarms.filter((swarm) => openRunStates.includes(swarm.state)),
    },
    {
      id: 'inactive',
      label: t('swarm.runs.inactive'),
      empty: t('swarm.runs.noInactive'),
      entries: swarms.filter((swarm) => !openRunStates.includes(swarm.state)),
    },
  ]);

  // The shown Run as its list entry (title, Swarm name, start time) and its
  // detail (state, participants, settings) together.
  const selectedRun = $derived(
    selectedSwarm
      ? {
          ...swarms.find((item) => item.id === selectedSwarm.id),
          ...selectedSwarm,
        }
      : null,
  );

  // The header offers Stop while anything works and Resume otherwise; failed
  // participants beside working peers get Resume in their notice instead of
  // a second header action.
  const working = $derived(
    Boolean(selectedSwarm) &&
      (WORKING_RUN_STATES.includes(selectedSwarm.state) ||
        (selectedSwarm.participants ?? []).some(
          (participant) => participantState(participant) === 'running',
        )),
  );

  const canResume = $derived(
    selectedSwarm &&
      !['stopping', 'preparing', 'deleting'].includes(selectedSwarm.state) &&
      (selectedSwarm.participants ?? []).some(
        (participant) =>
          !participant.run_active &&
          resumableParticipantState(participant.state),
      ),
  );

  const tabs = $derived([
    { id: 'board', label: t('swarm.tabs.board') },
    { id: 'wiki', label: t('swarm.tabs.wiki') },
    { id: 'participants', label: t('swarm.tabs.activity') },
    { id: 'usage', label: t('swarm.tabs.usage') },
  ]);

  // Why Start cannot run now; the fields' own errors show on a click.
  const startDisabledReason = $derived(
    loading && !profiles.length
      ? t('swarm.loading')
      : !profiles.length
        ? t('swarm.start.noProfiles')
        : directoryLoading
          ? t('swarm.start.directoryLoading')
          : '',
  );

  // The settings draft with its batching limits as numbers, the messages of
  // the limits outside their range by field (shown under those fields), and
  // the changes against the Run's settings.
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

  function applyContext(next) {
    setApplicationTimeZone(next.timezone);
    document.documentElement.lang = init(next.locale);
    for (const [name, value] of Object.entries(next.theme ?? {}))
      document.documentElement.style.setProperty(
        name.startsWith('--') ? name : `--${name}`,
        value,
      );
  }

  // Reloads everything the page shows. `background` marks a refresh no user
  // action asked for, which reloads a visible Usage report at most every
  // USAGE_REFRESH_INTERVAL_MS (`pageUsage.svelte.js`). A failure shows as
  // `refreshError` until a later refresh succeeds.
  async function refresh({ background = false } = {}) {
    const request = ++overviewRequest;
    const selection = selectionRequest;
    const failures = refreshFailures;
    // Invalidation must not unmount an active form or Run inspection.
    loading = loading && !editor;
    board.forgetPageReferences();
    try {
      await Promise.all([loadProfiles(), loadSwarms()]);
      if (disposed || request !== overviewRequest) return;
      if (
        selection === selectionRequest &&
        selectedSwarm &&
        pending !== 'delete'
      )
        await selectSwarm(selectedSwarm.id, { silent: true, background });
      if (disposed || request !== overviewRequest) return;
      if (failures === refreshFailures) refreshError = '';
    } catch (cause) {
      if (disposed || request !== overviewRequest) return;
      refreshFailed(cause);
    } finally {
      if (!disposed && request === overviewRequest) loading = false;
    }
  }

  // Shows a failed refresh, or a failed read the page started on its own, as
  // `refreshError`; a user action's `error` stays as it is.
  function refreshFailed(cause) {
    if (disposed) return;
    refreshFailures += 1;
    refreshError = cause?.message
      ? t('swarm.refreshError', { error: failureText(cause) })
      : t('swarm.loadError');
  }

  // The refresh error's Retry: a user action, so it also clears `error`.
  function retry() {
    error = '';
    return refresh();
  }

  // Reloads what a coalesced burst of invalidations can affect: profiles for
  // `profiles`, the Run list for `swarms`, and the selected Swarm with the
  // changed parts of its visible tab when any change names it. Anything else
  // refreshes the page.
  async function refreshChanges(changes) {
    if (everythingChanged(changes)) return refresh({ background: true });
    const request = ++changesRequest;
    const failures = refreshFailures;
    const selected =
      selectedSwarm &&
      pending !== 'delete' &&
      swarmChanged(changes, selectedSwarm.id)
        ? selectedSwarm
        : null;
    if (selected && swarmChanged(changes, selected.id, 'wiki'))
      board.forgetPageReferences();
    try {
      await Promise.all([
        changes.has('profiles') ? loadProfiles() : null,
        changes.has('swarms') ? loadSwarms() : null,
        selected
          ? selectSwarm(selected.id, {
              silent: true,
              background: true,
              changes,
            })
          : null,
      ]);
      if (disposed || request !== changesRequest) return;
      if (failures === refreshFailures) refreshError = '';
    } catch (cause) {
      if (disposed || request !== changesRequest) return;
      refreshFailed(cause);
    }
  }

  async function loadProfiles() {
    const request = ++profilesRequest;
    const result = await call('profiles.list', { limit: 100 });
    if (disposed || request !== profilesRequest) return;
    profiles = page(result);
    profilesCursor = result.cursor ?? null;
    profilesLoaded = true;
    if (routeWaits) void applyRoute(route);
    const previousProfile = selectedProfile;
    if (!selectedProfile) selectedProfile = profiles[0] ?? null;
    if (selectedProfile)
      selectedProfile =
        profiles.find((item) => item.id === selectedProfile.id) ??
        profiles[0] ??
        null;
    // Another chosen Swarm, or on the start page a default directory that
    // changed or failed to load and that the user has not edited, loads the
    // form's directory again.
    if (
      previousProfile?.id !== selectedProfile?.id ||
      (!editor &&
        !selectedSwarm &&
        sameServerPath(runDirectory, defaultDirectory) &&
        (directoryFailed ||
          JSON.stringify(previousProfile?.working_directory) !==
            JSON.stringify(selectedProfile?.working_directory)))
    )
      void selectRunProfile(selectedProfile, { background: true });
  }

  async function loadSwarms() {
    const request = ++swarmsRequest;
    const result = await call('swarms.list', { limit: 100 });
    if (disposed || request !== swarmsRequest) return;
    swarms = page(result);
    swarmsCursor = result.cursor ?? null;
  }

  // `changes` limits a background reload of the visible tab to what changed;
  // `tab` is the tab to show with the Swarm. A `silent` (background) reload
  // leaves its failure to the refresh that ran it.
  async function selectSwarm(
    id,
    { silent = false, background = false, changes = null, tab = null } = {},
  ) {
    const request = ++selectionRequest;
    if (!silent) {
      error = '';
      activity.leaveActivity();
      board.composeOpen = false;
    }
    try {
      // A Swarm's prompt and saved profile/configuration never change. Keep
      // their first snapshot while refreshing the complete mutable state.
      const retained = selectedSwarm?.id === id ? selectedSwarm : null;
      const snapshot = (
        await call('swarms.get', {
          swarm_id: id,
          ...(retained ? { include_snapshot: false } : {}),
        })
      ).swarm;
      if (disposed || request !== selectionRequest) return;
      const swarm = retained ? { ...retained, ...snapshot } : snapshot;
      if (selectedSwarm?.id !== id) {
        board.reset(swarm);
        usage.reset();
      }
      selectedSwarm = swarm;
      if (tab) activeTab = tab;
      if (!silent) editor = null;
      // The inspected participant, also before its History loaded, so a
      // refresh reads again what a failed read left out.
      const inspected = swarm.participants?.find(
        (item) => item.id === activity.selectedParticipant?.id,
      );
      if (silent && inspected) {
        void activity.inspectParticipant(inspected, {
          activate: false,
          preserve: true,
        });
      }
      await loadVisibleTab(swarm, { background, changes });
    } catch (cause) {
      if (disposed || request !== selectionRequest) return;
      if (silent) throw cause;
      error = failureText(cause);
    }
  }

  // Loads the visible tab, or with `changes` only its parts that changed.
  async function loadVisibleTab(
    swarm = selectedSwarm,
    { background = false, changes = null } = {},
  ) {
    if (!swarm) return;
    if (background) usage.noteChange();
    if (activeTab === 'board') await board.reload(swarm, changes);
    else if (activeTab === 'usage')
      await (background ? usage.schedule(swarm) : usage.load(swarm));
  }

  // The next page of a list, one request at a time. A reload of the list
  // meanwhile drops the page, and entries the list already holds are not
  // added twice.
  async function loadMoreProfiles() {
    if (!profilesCursor || moreProfilesLoading) return;
    const request = profilesRequest;
    moreProfilesLoading = true;
    error = '';
    try {
      const result = await call('profiles.list', {
        limit: 100,
        cursor: profilesCursor,
      });
      if (disposed || request !== profilesRequest) return;
      profiles = appendNew(profiles, page(result));
      profilesCursor = result.cursor ?? null;
    } catch (cause) {
      if (!disposed) error = failureText(cause);
    } finally {
      if (!disposed) moreProfilesLoading = false;
    }
  }

  async function loadMoreSwarms() {
    if (!swarmsCursor || moreSwarmsLoading) return;
    const request = swarmsRequest;
    moreSwarmsLoading = true;
    error = '';
    try {
      const result = await call('swarms.list', {
        limit: 100,
        cursor: swarmsCursor,
      });
      if (disposed || request !== swarmsRequest) return;
      swarms = appendNew(swarms, page(result));
      swarmsCursor = result.cursor ?? null;
    } catch (cause) {
      if (!disposed) error = failureText(cause);
    } finally {
      if (!disposed) moreSwarmsLoading = false;
    }
  }

  async function saveProfile(profile) {
    const saved = await call('profiles.save', {
      profile,
      expected_revision: profile.revision || null,
    });
    if (!profile.id) {
      profiles = [...profiles, saved.profile];
      selectedProfile = saved.profile;
      // The new profile's editor becomes its saved profile's editor.
      if (editor === 'new') {
        editor = saved.profile;
        syncRoute({ step: false });
      }
    } else {
      profiles = profiles.map((item) =>
        item.id === saved.profile.id ? saved.profile : item,
      );
    }
    selectedProfile = saved.profile;
    return saved.profile;
  }

  function openProfile(profile = 'new') {
    return stepTo(`/profiles/${profile === 'new' ? 'new' : profile.id}`);
  }

  // Opens the profile editor once the catalog it needs has loaded; only a
  // new Swarm marks the pane's New Swarm action as busy meanwhile.
  async function showProfile(profile) {
    const request = ++selectionRequest;
    const opening = profile === 'new' ? 'new-profile' : 'profile';
    pending = opening;
    error = '';
    try {
      const nextCatalog = (await call('catalog'))?.catalog ?? {};
      if (disposed || request !== selectionRequest) return;
      catalog = nextCatalog;
      activity.leaveActivity();
      selectedSwarm = null;
      if (profile !== 'new') selectedProfile = profile;
      editorKey += 1;
      editor = profile;
    } catch (cause) {
      if (!disposed && request === selectionRequest) error = failureText(cause);
    } finally {
      if (pending === opening) pending = '';
    }
  }

  // The start form with `profile` chosen and its goal focused.
  function startRunWith(profile) {
    return navigate(async () => {
      error = '';
      startErrors = {};
      void selectRunProfile(profile);
      focusRequest = { kind: 'goal' };
      if (editor || selectedSwarm) await stepTo('');
    });
  }

  function confirmProfileDelete(profile) {
    error = '';
    deleteError = '';
    profileDeleteCandidate = profile;
  }

  function confirmRunDelete(run) {
    error = '';
    deleteError = '';
    swarmDeleteCandidate = run;
  }

  // Closes a deletion confirmation unless its deletion is running.
  function cancelDelete() {
    if (pending === 'delete') return;
    profileDeleteCandidate = null;
    swarmDeleteCandidate = null;
  }

  // The pane row after the row of `id` (the one before it at the end of the
  // pane), which takes focus once that row is gone. `kind` is 'run' or
  // 'profile', after the rows' `data-run-id` and `data-profile-id`.
  function paneNeighbor(kind, id) {
    const key = `${kind}Id`;
    const rows = [...document.querySelectorAll(`[data-${kind}-id]`)];
    const index = rows.findIndex((row) => row.dataset[key] === id);
    if (index < 0) return null;
    return (rows[index + 1] ?? rows[index - 1])?.dataset[key] ?? null;
  }

  // Focuses the pane row of `id`, or New Run when there is none; a narrow
  // page hides the pane, and its picker takes the focus instead.
  async function focusPaneRow(kind, id) {
    await tick();
    if (disposed) return;
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
    if (!candidate || pending) return;
    pending = 'delete';
    deleteError = '';
    const next = paneNeighbor('profile', candidate.id);
    // An autosave may have advanced the revision while the dialog was open.
    const current =
      profiles.find((item) => item.id === candidate.id) ?? candidate;
    try {
      await call('profiles.delete', {
        profile_id: candidate.id,
        expected_revision: current.revision,
      });
      if (editor?.id === candidate.id) {
        // Close without the navigation flush: the draft belongs to the
        // deleted Swarm and must not be saved again.
        selectionRequest += 1;
        editor = null;
        syncRoute({ step: false });
      }
      if (selectedProfile?.id === candidate.id) void selectRunProfile(null);
      profileDeleteCandidate = null;
      await refresh();
      await focusPaneRow('profile', next);
    } catch (cause) {
      deleteError = failureText(cause);
    } finally {
      pending = '';
    }
  }

  async function deleteSwarm() {
    const candidate = swarmDeleteCandidate;
    if (!candidate || pending) return;
    pending = 'delete';
    deleteError = '';
    const next = paneNeighbor('run', candidate.id);
    try {
      // An open Run whose participants are all idle offers Resume, not Stop;
      // deletion closes it first because the Store deletes only stopped Runs.
      // A retry after a failed deletion starts from the stopped state.
      if (canStop(candidate.state)) {
        const stopped = await call('swarms.stop', {
          swarm_id: candidate.id,
          request_id: requestId(),
        });
        swarmDeleteCandidate = { ...candidate, state: stopped.state };
      }
      await call('swarms.delete', { swarm_id: candidate.id });
      if (selectedSwarm?.id === candidate.id) {
        showStart();
        syncRoute({ step: false });
      }
      swarmDeleteCandidate = null;
      await refresh();
      await focusPaneRow('run', next);
    } catch (cause) {
      deleteError = failureText(cause);
    } finally {
      pending = '';
    }
  }

  // Validates the form, showing each problem at its field and focusing the
  // first, then starts the Run and shows it with its header focused.
  // Names the Swarm's Models the catalog no longer offers, when it knows them.
  function unavailableModelsText(profile) {
    const known = (catalog.models ?? []).map((model) => model.id);
    const missing = (profile?.participants ?? [])
      .map((row) => row.model)
      .filter(
        (model, index, models) =>
          model && !known.includes(model) && models.indexOf(model) === index,
      );
    return missing.length
      ? t('swarm.error.model_unavailable.named', {
          models: missing.map((model) => `“${model}”`).join(', '),
        })
      : null;
  }

  async function startSwarm() {
    error = '';
    const errors = {
      profile: selectedProfile ? '' : t('swarm.start.profileRequired'),
      directory:
        directoryLoading || runDirectory.trim()
          ? ''
          : t('swarm.start.directoryRequired'),
      goal: goal.trim() ? '' : t('swarm.start.goalRequired'),
    };
    startErrors = Object.fromEntries(
      Object.entries(errors).filter(([, message]) => message),
    );
    const invalid = START_FIELDS.find(([field]) => errors[field]);
    if (invalid) {
      await tick();
      document.getElementById(invalid[1])?.focus();
      return;
    }
    if (directoryLoading || pending) return;
    pending = 'start';
    let missingDirectory = false;
    try {
      const result = await call('swarms.start', {
        profile_id: selectedProfile.id,
        expected_profile_revision: selectedProfile.revision,
        prompt: goal,
        request_id: requestId(),
        // The default spelled another way (`C:/work/`) is still the default.
        ...(sameServerPath(runDirectory, defaultDirectory)
          ? {}
          : { working_directory: runDirectory }),
      });
      goal = '';
      const id = result.swarm_id ?? result.id;
      focusRequest = { kind: 'run', id };
      await refresh();
      await stepTo(swarmRoute(id));
    } catch (cause) {
      // A folder the server does not find is a problem of its field.
      missingDirectory = cause?.message === 'directory_not_found';
      if (missingDirectory) startErrors = { directory: failureText(cause) };
      else if (cause?.message === 'model_unavailable')
        error = unavailableModelsText(selectedProfile) ?? failureText(cause);
      else error = failureText(cause);
    } finally {
      pending = '';
    }
    // Once the fields are enabled again.
    if (missingDirectory) {
      await tick();
      document.getElementById('swarm-start-directory')?.focus();
    }
  }

  // Takes the focus request for `kind` (and the Run `id`), if one waits.
  function takeFocus(kind, id = undefined) {
    if (
      focusRequest?.kind !== kind ||
      (id !== undefined && focusRequest.id !== id)
    )
      return false;
    focusRequest = null;
    return true;
  }

  async function lifecycle(operation, participantId = null) {
    if (!selectedSwarm) return;
    pending = operation;
    error = '';
    try {
      const result = await call(`swarms.${operation}`, {
        swarm_id: selectedSwarm.id,
        ...(participantId ? { participant_id: participantId } : {}),
        request_id: requestId(),
      });
      await refresh();
      showResumeFailures(result);
    } catch (cause) {
      error = failureText(cause);
    } finally {
      pending = '';
    }
  }

  function showResumeFailures(result) {
    if (result.resume_failed) {
      error = t('swarm.post.resumeFailed');
      return;
    }
    const failed = (result.runs ?? []).filter((run) => run.error);
    if (failed.length) {
      const names = failed.map(
        (run) =>
          selectedSwarm?.participants?.find(
            (participant) => participant.id === run.participant_id,
          )?.display_name ?? run.participant_id,
      );
      error = t('swarm.resume.failed', { names: names.join(', ') });
    }
  }

  // The actions of a Run, in its header menu and on its pane row: its
  // communication settings and details, then its deletion once nothing in
  // it works any more.
  function runMenu(run) {
    const busy =
      run.id === selectedSwarm?.id
        ? working
        : WORKING_RUN_STATES.includes(run.state);
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
          disabled: busy || Boolean(pending),
          hint: busy ? t('swarm.deleteRun.stopFirst') : undefined,
          onSelect: () => confirmRunDelete(run),
        },
      ],
    };
  }

  // The actions of a Swarm (profile) on its pane row.
  function profileMenu(profile) {
    return {
      label: t('swarm.sidebar.profileActions', { name: profile.name }),
      items: [
        {
          id: 'edit',
          label: t('common.edit'),
          group: 'profile',
          onSelect: () => void navigate(() => openProfile(profile)),
        },
        {
          id: 'start',
          label: t('swarm.sidebar.startRun'),
          group: 'profile',
          onSelect: () => void startRunWith(profile),
        },
        {
          id: 'delete',
          label: t('swarm.menu.delete'),
          danger: true,
          group: 'delete',
          disabled: Boolean(pending),
          onSelect: () => confirmProfileDelete(profile),
        },
      ],
    };
  }

  // Opens a Run's communication settings or details. The shown Run has its
  // detail at hand; another Run's (from its pane row) loads first.
  async function openRunDialog(kind, run) {
    const request = ++dialogRequest;
    error = '';
    let detail = selectedSwarm?.id === run.id ? selectedSwarm : null;
    if (!detail) {
      try {
        detail = (await call('swarms.get', { swarm_id: run.id })).swarm;
      } catch (cause) {
        if (!disposed && request === dialogRequest) error = failureText(cause);
        return;
      }
      if (disposed || request !== dialogRequest) return;
    }
    const record = {
      ...swarms.find((item) => item.id === run.id),
      ...detail,
    };
    if (kind === 'settings') openDelivery(record);
    else detailsRun = record;
  }

  // A Run without delivery settings has nothing to edit.
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
    if (pending === 'settings') return;
    settingsRun = null;
    deliveryDraft = null;
    settingsError = '';
  }

  // Applies the draft; a failure stays in the dialog for a retry. The shown
  // Run takes its new settings at once, other Runs with their next refresh.
  async function applyDelivery() {
    const run = settingsRun;
    const check = deliveryCheck;
    if (!run || !check || check.invalid.length || pending) return;
    pending = 'settings';
    settingsError = '';
    try {
      await call('swarms.settings', {
        swarm_id: run.id,
        delivery: JSON.parse(JSON.stringify(check.value)),
        expected_revision: run.settings_revision,
        request_id: requestId(),
      });
    } catch (cause) {
      if (!disposed) settingsError = failureText(cause);
      return;
    } finally {
      pending = '';
    }
    if (disposed) return;
    closeSettings();
    if (selectedSwarm?.id !== run.id) return;
    try {
      const swarm = (await call('swarms.get', { swarm_id: run.id })).swarm;
      if (!disposed && selectedSwarm?.id === run.id) selectedSwarm = swarm;
    } catch (cause) {
      if (selectedSwarm?.id === run.id) refreshFailed(cause);
    }
  }

  onMount(() => {
    const startupTimeout = setTimeout(() => {
      loading = false;
      refreshError = t('swarm.hostUnavailable');
    }, 10_000);
    const clock = setInterval(() => (now = Date.now()), CLOCK_INTERVAL_MS);
    let initialized = false;
    const offContext = client.onContext((next) => {
      clearTimeout(startupTimeout);
      applyContext(next);
      const nextRoute = typeof next.route === 'string' ? next.route : '';
      if (!initialized) {
        initialized = true;
        connected = true;
        // The host answered after all.
        refreshError = '';
        void applyRoute(nextRoute);
        void backgroundRefresh.run();
      } else if (nextRoute !== route) void applyRoute(nextRoute);
    });
    const offInvalidation = client.onInvalidation((invalidation) =>
      backgroundRefresh.schedule(invalidation?.change ?? null),
    );
    return () => {
      disposed = true;
      backgroundRefresh.destroy();
      usage.destroy();
      selectionRequest += 1;
      activity.destroy();
      clearTimeout(startupTimeout);
      clearInterval(clock);
      offContext();
      offInvalidation();
      client.dispose();
    };
  });
  const activity = createSwarmPageActivity({
    get model() {
      return model;
    },
  });
  const model = {
    get client() {
      return client;
    },
    get loading() {
      return loading;
    },
    get error() {
      return error;
    },
    set error(value) {
      error = value;
    },
    get refreshError() {
      return refreshError;
    },
    refreshFailed,
    get connected() {
      return connected;
    },
    get now() {
      return now;
    },
    get profiles() {
      return profiles;
    },
    get catalog() {
      return catalog;
    },
    set catalog(value) {
      catalog = value;
    },
    get selectedProfile() {
      return selectedProfile;
    },
    get selectedSwarm() {
      return selectedSwarm;
    },
    get selectedRun() {
      return selectedRun;
    },
    get profilesCursor() {
      return profilesCursor;
    },
    get swarmsCursor() {
      return swarmsCursor;
    },
    get moreProfilesLoading() {
      return moreProfilesLoading;
    },
    get moreSwarmsLoading() {
      return moreSwarmsLoading;
    },
    get activeTab() {
      return activeTab;
    },
    openTab,
    get editor() {
      return editor;
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
    get deliveryErrors() {
      return deliveryErrors;
    },
    get deliveryChangeLines() {
      return deliveryChangeLines;
    },
    get settingsError() {
      return settingsError;
    },
    get detailsRun() {
      return detailsRun;
    },
    closeDetails() {
      detailsRun = null;
    },
    get goal() {
      return goal;
    },
    set goal(value) {
      goal = value;
      clearStartErrors('goal');
    },
    get runDirectory() {
      return runDirectory;
    },
    set runDirectory(value) {
      runDirectory = value;
      clearStartErrors('directory');
    },
    get directoryLoading() {
      return directoryLoading;
    },
    get startErrors() {
      return startErrors;
    },
    get startDisabledReason() {
      return startDisabledReason;
    },
    get pending() {
      return pending;
    },
    get focusRequest() {
      return focusRequest;
    },
    takeFocus,
    get profileEditor() {
      return profileEditor;
    },
    set profileEditor(value) {
      profileEditor = value;
    },
    get newProfileDraft() {
      return newProfileDraft;
    },
    set newProfileDraft(value) {
      newProfileDraft = value;
    },
    get wikiPanel() {
      return wikiPanel;
    },
    set wikiPanel(value) {
      wikiPanel = value;
    },
    get editorKey() {
      return editorKey;
    },
    get disposed() {
      return disposed;
    },
    navigate,
    openStart,
    selectRunProfile,
    call,
    get runGroups() {
      return runGroups;
    },
    get working() {
      return working;
    },
    get canResume() {
      return canResume;
    },
    get tabs() {
      return tabs;
    },
    refresh,
    retry,
    openSwarm,
    loadMoreProfiles,
    loadMoreSwarms,
    saveProfile,
    openProfile,
    startRunWith,
    confirmProfileDelete,
    confirmRunDelete,
    cancelDelete,
    deleteProfile,
    deleteSwarm,
    startSwarm,
    lifecycle,
    runMenu,
    profileMenu,
    openRunDialog,
    changeDelivery,
    closeSettings,
    applyDelivery,
    board,
    activity,
    usage,
  };
  return model;
}
