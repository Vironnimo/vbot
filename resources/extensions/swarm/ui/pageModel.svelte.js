import { t, init } from '../../../../webui/src/lib/i18n.js';
import { formatAbsoluteTime } from '../../../../webui/src/lib/timeText.js';
import {
  canStop,
  participantState,
  resumableParticipantState,
  page,
  requestId,
} from './pagePresentation.js';
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

// The page's state across its areas: the profile and Run lists, the shown
// Swarm or profile with its route and tab, the start form, Run lifecycle and
// deletion, the communication settings draft, refreshes and the bridge
// lifetime. The Board, Activity and Usage areas keep their own state and
// requests in `board`, `activity` and `usage`.
export function createSwarmPageModel(host) {
  // Exists from the start, so the page root can hand its layer registry to
  // the page's dialogs before any of them mounts.
  const client = host.bridgeClient ?? createExtensionPageClient();

  const call = (operation, arguments_ = {}) =>
    client.operation(operation, arguments_);

  let loading = $state(true);

  let error = $state('');

  let profiles = $state([]);

  let swarms = $state([]);

  let catalog = $state({});

  let selectedProfile = $state(null);

  let selectedSwarm = $state(null);

  let profilesCursor = $state(null);

  let swarmsCursor = $state(null);

  let activeTab = $state('board');

  let editor = $state(null);

  let profileDeleteCandidate = $state(null);

  let swarmDeleteCandidate = $state(null);

  let deleteError = $state('');

  let settingsOpen = $state(false);

  let deliveryDraft = $state(null);

  let profileSnapshotOpen = $state(false);

  let goal = $state('');

  let runDirectory = $state('');

  let defaultDirectory = $state('');

  let directoryLoading = $state(false);

  let pending = $state('');

  let profileEditor = $state(null);

  let wikiPanel = $state(null);

  let editorKey = $state(0);

  let selectionRequest = 0;

  let overviewRequest = 0;

  let changesRequest = 0;

  let profilesRequest = 0;

  let swarmsRequest = 0;

  let directoryRequest = 0;

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
    // A step to the Board that loads nothing; a revealed post loads only
    // what it needs.
    stepToBoard() {
      activeTab = 'board';
      syncRoute({ step: true });
    },
    async openWikiPage(id) {
      openTab('wiki');
      await tick();
      await wikiPanel?.openPage(id);
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
    get error() {
      return error;
    },
    set error(value) {
      error = value;
    },
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
      if (!disposed) error = cause.message;
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
        if (!disposed) error = cause.message;
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
      const profile =
        target.profile === 'new'
          ? 'new'
          : profiles.find((item) => item.id === target.profile);
      if (!profile) return profilesLoaded;
      if (editor === profile || (editor?.id && editor.id === profile.id))
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

  // Dialogs act on the shown record, so showing another one closes them.
  function closeDialogs() {
    board.composeOpen = false;
    settingsOpen = false;
    profileSnapshotOpen = false;
    if (pending === 'delete') return;
    profileDeleteCandidate = null;
    swarmDeleteCandidate = null;
  }

  function showStart() {
    selectionRequest += 1;
    editor = null;
    selectedSwarm = null;
    activity.leaveActivity();
    void selectRunProfile(selectedProfile);
  }

  function newSwarm() {
    return navigate(() => stepTo(''));
  }

  function openSwarm(id) {
    return stepTo(swarmRoute(id, activeTab));
  }

  function showTab(tab) {
    if (tab === activeTab) return;
    activeTab = tab;
    void loadVisibleTab().catch((cause) => {
      if (!disposed) error = cause.message;
    });
  }

  function openTab(tab) {
    showTab(tab);
    syncRoute({ step: true });
  }

  async function selectRunProfile(profile) {
    const request = ++directoryRequest;
    selectedProfile = profile;
    runDirectory = '';
    defaultDirectory = '';
    directoryLoading = false;
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
      if (!disposed && request === directoryRequest) error = cause.message;
    } finally {
      if (!disposed && request === directoryRequest) directoryLoading = false;
    }
  }

  const runGroups = $derived([
    {
      id: 'active',
      label: t('swarm.runs.active'),
      empty: t('swarm.runs.noActive'),
      entries: swarms.filter((swarm) =>
        ['preparing', 'running', 'stopping'].includes(swarm.state),
      ),
    },
    {
      id: 'inactive',
      label: t('swarm.runs.inactive'),
      empty: t('swarm.runs.noInactive'),
      entries: swarms.filter(
        (swarm) => !['preparing', 'running', 'stopping'].includes(swarm.state),
      ),
    },
  ]);

  // The header offers Stop while anything works and Resume otherwise, so one
  // failed participant beside running peers does not add a second action.
  const working = $derived(
    Boolean(selectedSwarm) &&
      (['preparing', 'running', 'stopping'].includes(selectedSwarm.state) ||
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

  // In the host's timezone, which `applyContext` makes the application zone.
  const date = (value) => formatAbsoluteTime(value);

  const settingChanges = $derived(
    deliveryDraft && selectedSwarm
      ? [
          ...['main', 'discussion', 'ping'].flatMap((route) =>
            ['mode', 'wake_idle']
              .filter(
                (field) =>
                  deliveryDraft[route]?.[field] !==
                  selectedSwarm.delivery?.[route]?.[field],
              )
              .map((field) => ({
                route,
                field,
                before: String(selectedSwarm.delivery?.[route]?.[field]),
                after: String(deliveryDraft[route]?.[field]),
              })),
          ),
          ...['coalesce_ms', 'batch_messages', 'batch_chars']
            .filter(
              (field) =>
                deliveryDraft[field] !== selectedSwarm.delivery?.[field],
            )
            .map((field) => ({
              route: t('swarm.communication.advanced'),
              field,
              before: String(selectedSwarm.delivery?.[field]),
              after: String(deliveryDraft[field]),
            })),
        ]
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
  // USAGE_REFRESH_INTERVAL_MS (`pageUsage.svelte.js`).
  async function refresh({ background = false } = {}) {
    const request = ++overviewRequest;
    const selection = selectionRequest;
    // Invalidation must not unmount an active form or Run inspection.
    loading = loading && !editor;
    board.forgetPageReferences();
    error = '';
    try {
      await Promise.all([loadProfiles(), loadSwarms()]);
      if (disposed || request !== overviewRequest) return;
      if (
        selection === selectionRequest &&
        selectedSwarm &&
        pending !== 'delete'
      )
        await selectSwarm(selectedSwarm.id, { silent: true, background });
    } catch (cause) {
      if (disposed || request !== overviewRequest) return;
      error = cause.message ?? t('swarm.loadError');
    } finally {
      if (!disposed && request === overviewRequest) loading = false;
    }
  }

  // Reloads what a coalesced burst of invalidations can affect: profiles for
  // `profiles`, the Run list for `swarms`, and the selected Swarm with the
  // changed parts of its visible tab when any change names it. Anything else
  // refreshes the page.
  async function refreshChanges(changes) {
    if (everythingChanged(changes)) return refresh({ background: true });
    const request = ++changesRequest;
    const selected =
      selectedSwarm &&
      pending !== 'delete' &&
      swarmChanged(changes, selectedSwarm.id)
        ? selectedSwarm
        : null;
    if (selected && swarmChanged(changes, selected.id, 'wiki'))
      board.forgetPageReferences();
    error = '';
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
    } catch (cause) {
      if (disposed || request !== changesRequest) return;
      error = cause.message ?? t('swarm.loadError');
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
    if (
      previousProfile?.id !== selectedProfile?.id ||
      (!editor &&
        !selectedSwarm &&
        sameServerPath(runDirectory, defaultDirectory) &&
        JSON.stringify(previousProfile?.working_directory) !==
          JSON.stringify(selectedProfile?.working_directory))
    )
      void selectRunProfile(selectedProfile);
  }

  async function loadSwarms() {
    const request = ++swarmsRequest;
    const result = await call('swarms.list', { limit: 100 });
    if (disposed || request !== swarmsRequest) return;
    swarms = page(result);
    swarmsCursor = result.cursor ?? null;
  }

  // `changes` limits a background reload of the visible tab to what changed;
  // `tab` is the tab to show with the Swarm.
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
      const swarm = (await call('swarms.get', { swarm_id: id })).swarm;
      if (disposed || request !== selectionRequest) return;
      if (selectedSwarm?.id !== id) {
        board.reset(swarm);
        usage.reset();
      }
      selectedSwarm = swarm;
      if (tab) activeTab = tab;
      if (!silent) editor = null;
      const inspected = swarm.participants?.find(
        (item) => item.id === activity.history?.participant.id,
      );
      if (silent && inspected) {
        void activity.inspectParticipant(inspected, {
          activate: false,
          preserve: true,
        });
      }
      await loadVisibleTab(swarm, { background, changes });
    } catch (cause) {
      if (!disposed && request === selectionRequest) error = cause.message;
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

  async function loadMoreProfiles() {
    if (!profilesCursor) return;
    const result = await call('profiles.list', {
      limit: 100,
      cursor: profilesCursor,
    });
    profiles = [...profiles, ...page(result)];
    profilesCursor = result.cursor ?? null;
  }

  async function loadMoreSwarms() {
    if (!swarmsCursor) return;
    const result = await call('swarms.list', {
      limit: 100,
      cursor: swarmsCursor,
    });
    swarms = [...swarms, ...page(result)];
    swarmsCursor = result.cursor ?? null;
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

  async function showProfile(profile) {
    const request = ++selectionRequest;
    pending = 'profile';
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
      if (!disposed && request === selectionRequest) error = cause.message;
    } finally {
      if (pending === 'profile') pending = '';
    }
  }

  function confirmProfileDelete(profile) {
    deleteError = '';
    profileDeleteCandidate = profile;
  }

  async function deleteProfile() {
    const candidate = profileDeleteCandidate;
    if (!candidate || pending) return;
    pending = 'delete';
    deleteError = '';
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
    } catch (cause) {
      deleteError = cause.message;
    } finally {
      pending = '';
    }
  }

  async function deleteSwarm() {
    const candidate = swarmDeleteCandidate;
    if (!candidate || pending) return;
    pending = 'delete';
    deleteError = '';
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
    } catch (cause) {
      deleteError = cause.message;
    } finally {
      pending = '';
    }
  }

  async function startSwarm() {
    error = '';
    if (!selectedProfile || !goal.trim()) {
      error = t('swarm.start.validation');
      return;
    }
    if (directoryLoading || !runDirectory.trim()) {
      error = t('swarm.start.directoryRequired');
      return;
    }
    pending = 'start';
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
      await refresh();
      await stepTo(swarmRoute(result.swarm_id ?? result.id));
    } catch (cause) {
      error = cause.message;
    } finally {
      pending = '';
    }
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
      error = cause.message;
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

  function openDelivery() {
    deliveryDraft = JSON.parse(JSON.stringify(selectedSwarm.delivery));
    settingsOpen = true;
  }

  function changeDelivery(route, field, value) {
    deliveryDraft = {
      ...deliveryDraft,
      [route]: { ...deliveryDraft[route], [field]: value },
    };
  }

  function changeDeliverySetting(field, value) {
    deliveryDraft = { ...deliveryDraft, [field]: Number(value) };
  }

  async function applyDelivery() {
    if (!selectedSwarm || !deliveryDraft) return;
    pending = 'settings';
    try {
      const result = await call('swarms.settings', {
        swarm_id: selectedSwarm.id,
        delivery: JSON.parse(JSON.stringify(deliveryDraft)),
        expected_revision: selectedSwarm.settings_revision,
        request_id: requestId(),
      });
      selectedSwarm =
        result.swarm ??
        (await call('swarms.get', { swarm_id: selectedSwarm.id })).swarm;
      settingsOpen = false;
    } catch (cause) {
      error = cause.message;
    } finally {
      pending = '';
    }
  }

  onMount(() => {
    const startupTimeout = setTimeout(() => {
      loading = false;
      error = t('swarm.hostUnavailable');
    }, 10_000);
    let initialized = false;
    const offContext = client.onContext((next) => {
      clearTimeout(startupTimeout);
      applyContext(next);
      const nextRoute = typeof next.route === 'string' ? next.route : '';
      if (!initialized) {
        initialized = true;
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
    get profilesCursor() {
      return profilesCursor;
    },
    get swarmsCursor() {
      return swarmsCursor;
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
    set profileDeleteCandidate(value) {
      profileDeleteCandidate = value;
    },
    get swarmDeleteCandidate() {
      return swarmDeleteCandidate;
    },
    set swarmDeleteCandidate(value) {
      swarmDeleteCandidate = value;
    },
    get deleteError() {
      return deleteError;
    },
    set deleteError(value) {
      deleteError = value;
    },
    get settingsOpen() {
      return settingsOpen;
    },
    set settingsOpen(value) {
      settingsOpen = value;
    },
    get deliveryDraft() {
      return deliveryDraft;
    },
    get profileSnapshotOpen() {
      return profileSnapshotOpen;
    },
    set profileSnapshotOpen(value) {
      profileSnapshotOpen = value;
    },
    get goal() {
      return goal;
    },
    set goal(value) {
      goal = value;
    },
    get runDirectory() {
      return runDirectory;
    },
    set runDirectory(value) {
      runDirectory = value;
    },
    get directoryLoading() {
      return directoryLoading;
    },
    get pending() {
      return pending;
    },
    get profileEditor() {
      return profileEditor;
    },
    set profileEditor(value) {
      profileEditor = value;
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
    newSwarm,
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
    date,
    get settingChanges() {
      return settingChanges;
    },
    refresh,
    openSwarm,
    loadMoreProfiles,
    loadMoreSwarms,
    saveProfile,
    openProfile,
    confirmProfileDelete,
    deleteProfile,
    deleteSwarm,
    startSwarm,
    lifecycle,
    openDelivery,
    changeDelivery,
    changeDeliverySetting,
    applyDelivery,
    board,
    activity,
    usage,
  };
  return model;
}
