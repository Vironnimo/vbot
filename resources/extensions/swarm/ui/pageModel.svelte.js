import { createSwarmStartForm } from './pageStartForm.svelte.js';
import { createSwarmRunActions } from './pageRunActions.svelte.js';
import { t, init } from '../../../../webui/src/lib/i18n.js';
import { page, requestId } from './pagePresentation.js';
import {
  runGroups as projectRunGroups,
  selectedRun as projectSelectedRun,
  working as projectWorking,
  canResume as projectCanResume,
} from './runPresentation.js';
import { setApplicationTimeZone } from '../../../../webui/src/lib/dateTimePrefs.svelte.js';
import { onMount } from 'svelte';
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

// `list` followed by the entries of `more` it does not hold yet.
function appendNew(list, more) {
  const known = new Set(list.map((item) => item.id));
  return [...list, ...more.filter((item) => !known.has(item.id))];
}

// The page's state across its areas: the profile and Run lists, the shown
// Swarm or profile with its route and tab, refreshes and the bridge lifetime.
// The start form and Run actions own their state. The Board,
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

  let selectedSwarm = $state(null);

  let profilesCursor = $state(null);

  let swarmsCursor = $state(null);

  let moreProfilesLoading = $state(false);

  let moreSwarmsLoading = $state(false);

  let activeTab = $state('board');

  let editor = $state(null);

  let pending = $state('');

  // Where focus goes once a view shows: `{ kind: 'goal' }` for the start
  // form's goal, `{ kind: 'run', id }` for a Run's header.
  let focusRequest = $state(null);

  let profileEditor = $state(null);

  // A new Swarm's changed draft, kept while its editor is closed
  // (ProfileEditor's `newDraft`); the page's state ends with the page.
  let newProfileDraft = $state.raw(null);

  let wikiPanel = $state(null);

  // A Board citation can arrive before the Wiki's code finishes loading.
  // Keep the requested page until its panel mounts, only for this Swarm/tab.
  let wikiRequest = $state.raw(null);

  let editorKey = $state(0);

  let selectionRequest = 0;

  let overviewRequest = 0;

  let changesRequest = 0;

  let profilesRequest = 0;

  let swarmsRequest = 0;

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

  const start = createSwarmStartForm({
    get catalog() {
      return catalog;
    },
    get disposed() {
      return disposed;
    },
    get loading() {
      return loading;
    },
    get profiles() {
      return profiles;
    },
    get pending() {
      return pending;
    },
    set pending(value) {
      pending = value;
    },
    get error() {
      return error;
    },
    set error(value) {
      error = value;
    },
    get focusRequest() {
      return focusRequest;
    },
    set focusRequest(value) {
      focusRequest = value;
    },
    call: call,
    refresh: refresh,
    stepTo: stepTo,
    swarmRoute: (id, tab) => swarmRoute(id, tab),
    refreshFailed: refreshFailed,
  });

  const runActions = createSwarmRunActions({
    get selectedSwarm() {
      return selectedSwarm;
    },
    set selectedSwarm(value) {
      selectedSwarm = value;
    },
    get swarms() {
      return swarms;
    },
    get working() {
      return working;
    },
    get pending() {
      return pending;
    },
    set pending(value) {
      pending = value;
    },
    get error() {
      return error;
    },
    set error(value) {
      error = value;
    },
    get disposed() {
      return disposed;
    },
    get profiles() {
      return profiles;
    },
    get editor() {
      return editor;
    },
    set editor(value) {
      editor = value;
    },
    get selectionRequest() {
      return selectionRequest;
    },
    set selectionRequest(value) {
      selectionRequest = value;
    },
    get selectedProfile() {
      return start.selectedProfile;
    },
    selectRunProfile: start.selectRunProfile,
    navigate: navigate,
    openProfile: openProfile,
    startRunWith: startRunWith,
    showStart: showStart,
    syncRoute: syncRoute,
    refresh: refresh,
    refreshFailed: refreshFailed,
    call: call,
  });

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
        const swarmId = selectedSwarm?.id;
        if (!(await openTab('wiki')) || selectedSwarm?.id !== swarmId) return;
        wikiRequest = { swarmId, id };
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

  $effect(() => {
    const request = wikiRequest;
    if (!request) return;
    if (activeTab !== 'wiki' || selectedSwarm?.id !== request.swarmId) {
      wikiRequest = null;
      return;
    }
    if (!wikiPanel) return;
    wikiRequest = null;
    void wikiPanel.openPage(request.id).catch((cause) => {
      if (!disposed) error = failureText(cause);
    });
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
    runActions.closeDialogs();
  }

  // A new visit to the start page keeps the goal but no earlier field errors.
  function showStart() {
    selectionRequest += 1;
    editor = null;
    selectedSwarm = null;
    start.startErrors = {};
    activity.leaveActivity();
    void start.selectRunProfile(start.selectedProfile);
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

  // Open Runs (working, waiting for messages or needing the user) come
  // first, so a Run that needs attention never hides among finished ones.
  const runGroups = $derived(projectRunGroups(swarms));

  // The shown Run as its list entry (title, Swarm name, start time) and its
  // detail (state, participants, settings) together.
  const selectedRun = $derived(projectSelectedRun(swarms, selectedSwarm));

  // The header offers Stop while anything works and Resume otherwise; failed
  // participants beside working peers get Resume in their notice instead of
  // a second header action.
  const working = $derived(projectWorking(selectedSwarm));

  const canResume = $derived(projectCanResume(selectedSwarm));

  const tabs = $derived([
    { id: 'board', label: t('swarm.tabs.board') },
    { id: 'wiki', label: t('swarm.tabs.wiki') },
    { id: 'participants', label: t('swarm.tabs.activity') },
    { id: 'usage', label: t('swarm.tabs.usage') },
  ]);

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
    const previousProfile = start.selectedProfile;
    if (!start.selectedProfile) start.selectedProfile = profiles[0] ?? null;
    if (start.selectedProfile)
      start.selectedProfile =
        profiles.find((item) => item.id === start.selectedProfile.id) ??
        profiles[0] ??
        null;
    // Another chosen Swarm, or on the start page a default directory that
    // changed or failed to load and that the user has not edited, loads the
    // form's directory again.
    if (
      previousProfile?.id !== start.selectedProfile?.id ||
      (!editor &&
        !selectedSwarm &&
        sameServerPath(start.runDirectory, start.defaultDirectory) &&
        (start.directoryFailed ||
          JSON.stringify(previousProfile?.working_directory) !==
            JSON.stringify(start.selectedProfile?.working_directory)))
    )
      void start.selectRunProfile(start.selectedProfile, { background: true });
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
      start.selectedProfile = saved.profile;
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
    start.selectedProfile = saved.profile;
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
      if (profile !== 'new') start.selectedProfile = profile;
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
      start.startErrors = {};
      void start.selectRunProfile(profile);
      focusRequest = { kind: 'goal' };
      if (editor || selectedSwarm) await stepTo('');
    });
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
    start,
    runActions,
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
    lifecycle,
    board,
    activity,
    usage,
  };
  return model;
}
