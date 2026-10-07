import { SvelteDate, SvelteMap } from 'svelte/reactivity';
import { t, activeLocaleTag, init } from '../../../../webui/src/lib/i18n.js';
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
import { referenceLinks } from './referenceLinks.js';

// Background changes reload the Usage report at most this often; the last
// change of a burst is always followed by a reload.
const USAGE_REFRESH_INTERVAL_MS = 10_000;

export function createSwarmPageModel(host) {
  // Exists from the start, so the page root can hand its layer registry to
  // the page's dialogs before any of them mounts.
  const client = host.bridgeClient ?? createExtensionPageClient();

  let loading = $state(true);

  let error = $state('');

  let profiles = $state([]);

  let swarms = $state([]);

  let catalog = $state({});

  let selectedProfile = $state(null);

  let selectedSwarm = $state(null);

  let board = $state([]);

  let boardCursor = $state(null);

  let discussions = $state([]);

  let selectedDiscussion = $state('');

  let discussionCursor = $state(null);

  let profilesCursor = $state(null);

  let swarmsCursor = $state(null);

  let usage = $state(null);

  let participantUsage = $state([]);

  let activeTab = $state('board');

  let editor = $state(null);

  let deleteCandidate = $state(null);

  let swarmDeleteCandidate = $state(null);

  let deleteError = $state('');

  let settingsOpen = $state(false);

  let deliveryDraft = $state(null);

  let profileSnapshotOpen = $state(false);

  let goal = $state('');

  let runDirectory = $state('');

  let defaultDirectory = $state('');

  let directoryLoading = $state(false);

  let postText = $state('');

  let composeOpen = $state(false);

  let postRecipients = $state('');

  let replyTo = $state('');

  let posting = $state(false);
  let postMutation = null;

  let pending = $state('');

  let context = $state({ locale: 'en', timezone: 'UTC', theme: {} });

  let profileEditor = $state(null);

  let editorKey = $state(0);

  let selectionRequest = 0;

  let overviewRequest = 0;

  let changesRequest = 0;

  let profilesRequest = 0;

  let swarmsRequest = 0;

  // `board` holds the newest posts of `boardShown` (a Swarm and discussion)
  // with any earlier pages below them. Each full load of the newest page
  // counts up `boardVersion`; an earlier page or newer posts read for an older
  // version are dropped, since the newer full load includes them.
  let boardShown = null;

  let boardVersion = 0;

  // The full load in flight, which a read of newer posts waits for.
  let boardLoading = null;

  let discussionsRequest = 0;

  let usageRequest = 0;

  let usageFlight = null;

  // Counts background changes of the selected Swarm; an in-flight Usage
  // request is shared only while no change has arrived since it started.
  let usageChanges = 0;

  let usageStartedAt = -Infinity;

  let usageTimer = null;

  let usageLoading = $state(false);

  let directoryRequest = 0;

  let revealRequest = 0;

  // Tooltip content for "#N" and "wN" links, by "post:N" and "page:N".
  const references = new SvelteMap();

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

  const backgroundRefresh = createPageRefresh(refreshChanges);

  function navigate(action) {
    if (profileEditor) return profileEditor.requestTransition(action);
    if (host.wiki) return host.wiki.requestTransition(action);
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
    composeOpen = false;
    settingsOpen = false;
    profileSnapshotOpen = false;
    if (pending === 'delete') return;
    deleteCandidate = null;
    swarmDeleteCandidate = null;
  }

  function showStart() {
    selectionRequest += 1;
    editor = null;
    selectedSwarm = null;
    host.activity.leaveActivity();
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

  const call = (operation, arguments_ = {}) =>
    client.operation(operation, arguments_);

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

  const date = (value) =>
    value
      ? new Intl.DateTimeFormat(activeLocaleTag(), {
          dateStyle: 'medium',
          timeStyle: 'short',
          timeZone: context.timezone || 'UTC',
        }).format(new SvelteDate(value))
      : '';

  const discussionOptions = $derived(discussions);
  const discussionParticipants = $derived(
    (selectedSwarm?.participants ?? []).filter((participant) =>
      participant.discussion_ids?.includes(selectedDiscussion),
    ),
  );

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

  const usageRows = $derived(
    participantUsage.flatMap(({ participant, report }) => {
      const models = report?.usage?.activity?.models ?? [];
      return (models.length ? models : [null]).map((model, index) => ({
        id: `${participant.id}:${index}`,
        participant: participant.display_name,
        model,
        modelName: model ? model.model : participant.model,
        participantRows: index === 0 ? Math.max(models.length, 1) : 0,
        toolCalls: report?.usage?.activity?.tool_calls,
      }));
    }),
  );

  function applyContext(next) {
    context = next;
    setApplicationTimeZone(next.timezone);
    document.documentElement.lang = init(next.locale);
    for (const [name, value] of Object.entries(next.theme ?? {}))
      document.documentElement.style.setProperty(
        name.startsWith('--') ? name : `--${name}`,
        value,
      );
  }

  // Posts never change; a Wiki page's title and text can.
  function forgetPageReferences() {
    for (const key of references.keys())
      if (key.startsWith('page:')) references.delete(key);
  }

  // Reloads everything the page shows. `background` marks a refresh no user
  // action asked for, which reloads a visible Usage report at most every
  // USAGE_REFRESH_INTERVAL_MS.
  async function refresh({ background = false } = {}) {
    const request = ++overviewRequest;
    const selection = selectionRequest;
    // Invalidation must not unmount an active form or Run inspection.
    loading = loading && !editor;
    forgetPageReferences();
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
      forgetPageReferences();
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
      host.activity.leaveActivity();
      composeOpen = false;
    }
    try {
      const swarm = (await call('swarms.get', { swarm_id: id })).swarm;
      if (disposed || request !== selectionRequest) return;
      if (selectedSwarm?.id !== id) {
        references.clear();
        selectedDiscussion = swarm.main_discussion_id;
        discussions = [];
        clearBoard();
        usage = null;
        participantUsage = [];
      }
      selectedSwarm = swarm;
      if (tab) activeTab = tab;
      if (!silent) editor = null;
      const inspected = swarm.participants?.find(
        (item) => item.id === host.activity.history?.participant.id,
      );
      if (silent && inspected) {
        void host.activity.inspectParticipant(inspected, {
          activate: false,
          preserve: true,
        });
      }
      await loadVisibleTab(swarm, { background, changes });
    } catch (cause) {
      if (!disposed && request === selectionRequest) error = cause.message;
    }
  }

  // Loads the visible tab, or with `changes` only its parts that changed:
  // the discussions, and the posts newer than those the Board shows.
  async function loadVisibleTab(
    swarm = selectedSwarm,
    { background = false, changes = null } = {},
  ) {
    if (!swarm) return;
    if (background) usageChanges += 1;
    if (activeTab === 'board') {
      const changed = (resource) => swarmChanged(changes, swarm.id, resource);
      await Promise.all([
        changed('discussions') ? loadDiscussions(swarm) : null,
        !changed('posts')
          ? null
          : changes
            ? loadNewPosts(swarm)
            : loadBoard(swarm, selectedDiscussion),
      ]);
    } else if (activeTab === 'usage')
      await (background ? scheduleUsage(swarm) : loadUsage(swarm));
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

  async function loadDiscussions(swarm = selectedSwarm, cursor = null) {
    if (!swarm) return;
    const selection = selectionRequest;
    const request = ++discussionsRequest;
    const result = await call('board.list', {
      swarm_id: swarm.id,
      limit: 100,
      ...(cursor ? { cursor } : {}),
    });
    if (
      disposed ||
      selection !== selectionRequest ||
      request !== discussionsRequest ||
      selectedSwarm?.id !== swarm.id
    )
      return;
    const selected = discussions.find((item) => item.id === selectedDiscussion);
    discussions = cursor
      ? [
          ...new SvelteMap(
            [...discussions, ...page(result)].map((item) => [item.id, item]),
          ).values(),
        ]
      : page(result);
    if (selected && !discussions.some((item) => item.id === selected.id))
      discussions = [...discussions, selected];
    discussionCursor = result.cursor ?? null;
  }

  function clearBoard() {
    boardVersion += 1;
    boardShown = null;
    board = [];
    boardCursor = null;
  }

  const shows = (swarm, discussionId) =>
    boardShown?.swarmId === swarm.id &&
    boardShown.discussionId === discussionId;

  const boardCurrent = (version, swarm, discussionId) =>
    !disposed &&
    version === boardVersion &&
    selectedSwarm?.id === swarm.id &&
    selectedDiscussion === discussionId;

  // Loads the discussion's newest page, or with `cursor` the earlier page
  // below the posts the Board shows.
  async function loadBoard(
    swarm = selectedSwarm,
    discussionId = selectedDiscussion,
    cursor = null,
  ) {
    if (!swarm) return;
    const version = cursor ? boardVersion : ++boardVersion;
    const load = call('board.read', {
      swarm_id: swarm.id,
      discussion_id: discussionId,
      limit: 100,
      ...(cursor ? { cursor } : {}),
    });
    if (!cursor) boardLoading = load;
    let result;
    try {
      result = await load;
    } finally {
      if (boardLoading === load) boardLoading = null;
    }
    if (!boardCurrent(version, swarm, discussionId)) return;
    const posts = [...page(result)].reverse();
    if (cursor) {
      if (!shows(swarm, discussionId)) return;
      const oldest = board.at(-1)?.sequence ?? Infinity;
      board = [...board, ...posts.filter((post) => post.sequence < oldest)];
    } else {
      board = posts;
      boardShown = { swarmId: swarm.id, discussionId };
    }
    boardCursor = result.cursor ?? null;
  }

  // Puts the selected discussion's posts newer than the Board's newest on top.
  // A Board that does not show the discussion yet loads its newest page.
  async function loadNewPosts(swarm) {
    const discussionId = selectedDiscussion;
    // A full load in flight may have read before the newest posts.
    await boardLoading?.catch(() => {});
    if (!shows(swarm, discussionId)) return loadBoard(swarm, discussionId);
    const version = boardVersion;
    for (;;) {
      const result = await call('board.read', {
        swarm_id: swarm.id,
        discussion_id: discussionId,
        after: board[0]?.sequence ?? 0,
        limit: 100,
      });
      if (!boardCurrent(version, swarm, discussionId)) return;
      const posts = page(result);
      const newest = board[0]?.sequence ?? 0;
      board = [
        ...posts.filter((post) => post.sequence > newest).reverse(),
        ...board,
      ];
      if (!result.has_more || !posts.length) return;
    }
  }

  async function chooseDiscussion(id) {
    selectedDiscussion = id;
    clearBoard();
    await loadBoard(selectedSwarm, id);
  }

  async function openDiscussion(announcement) {
    if (!discussions.some((item) => item.id === announcement.discussion_id))
      discussions = [
        ...discussions,
        { id: announcement.discussion_id, title: announcement.title },
      ];
    try {
      await chooseDiscussion(announcement.discussion_id);
    } catch (cause) {
      error = cause.message;
    }
  }

  // A background change reloads Usage at once when the last request is old
  // enough, otherwise when USAGE_REFRESH_INTERVAL_MS since it have passed.
  function scheduleUsage(swarm) {
    const wait = usageStartedAt + USAGE_REFRESH_INTERVAL_MS - Date.now();
    if (wait <= 0) return loadUsage(swarm);
    usageTimer ??= setTimeout(() => {
      usageTimer = null;
      if (disposed || activeTab !== 'usage' || !selectedSwarm) return;
      void loadUsage(selectedSwarm).catch((cause) => {
        if (!disposed) error = cause.message;
      });
    }, wait);
  }

  async function loadUsage(swarm = selectedSwarm) {
    if (!swarm) return;
    clearTimeout(usageTimer);
    usageTimer = null;
    const request = ++usageRequest;
    usageLoading = true;
    if (
      usageFlight?.swarmId !== swarm.id ||
      usageFlight.changes !== usageChanges
    ) {
      const flight = {
        swarmId: swarm.id,
        changes: usageChanges,
        promise: call('swarms.usage', { swarm_id: swarm.id }),
      };
      usageFlight = flight;
      usageStartedAt = Date.now();
    }
    const flight = usageFlight;
    try {
      const report = await flight.promise;
      if (
        disposed ||
        request !== usageRequest ||
        selectedSwarm?.id !== swarm.id
      )
        return;
      usage = report;
      participantUsage = (swarm.participants ?? []).map((participant) => ({
        participant,
        report: {
          usage: report.usage?.participants?.find(
            (row) => row.participant_id === participant.id,
          ),
        },
      }));
    } finally {
      if (usageFlight === flight) usageFlight = null;
      if (!disposed && request === usageRequest) usageLoading = false;
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

  async function showProfile(profile) {
    const request = ++selectionRequest;
    pending = 'profile';
    error = '';
    try {
      const nextCatalog = (await call('catalog'))?.catalog ?? {};
      if (disposed || request !== selectionRequest) return;
      catalog = nextCatalog;
      host.activity.leaveActivity();
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
    deleteCandidate = profile;
  }

  async function deleteProfile() {
    const candidate = deleteCandidate;
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
      deleteCandidate = null;
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

  async function post() {
    if (posting || !selectedSwarm || !postText.trim()) return;
    const payload = {
      swarm_id: selectedSwarm.id,
      discussion_id: selectedDiscussion,
      text: postText,
      ...(replyTo.trim() ? { reply_to: replyTo.trim() } : {}),
      ...(postRecipients.trim()
        ? {
            recipients: postRecipients
              .split(',')
              .map((item) => item.trim())
              .filter(Boolean),
          }
        : {}),
    };
    const fingerprint = JSON.stringify(payload);
    if (postMutation?.fingerprint !== fingerprint)
      postMutation = { fingerprint, id: requestId() };
    posting = true;
    error = '';
    try {
      const result = await call('board.post', {
        ...payload,
        request_id: postMutation.id,
      });
      postMutation = null;
      postText = '';
      postRecipients = '';
      replyTo = '';
      composeOpen = false;
      await refresh();
      showResumeFailures(result);
    } catch (cause) {
      error = cause.message;
    } finally {
      posting = false;
    }
  }

  function contentLinks(node, { references: linked = true } = {}) {
    node.addEventListener('click', openContentLink);
    const numbers = linked
      ? referenceLinks(node, {
          target: referenceTarget,
          describe: describeReference,
          loading: (reference) => ({
            title: reference,
            text: t('swarm.references.loading'),
          }),
        })
      : null;
    return {
      destroy: () => {
        node.removeEventListener('click', openContentLink);
        numbers?.destroy();
      },
    };
  }

  // "#N" names a post and "wN" a Wiki page. A post cites only earlier posts;
  // other Markdown cites any post or page that exists.
  function referenceTarget(kind, number, text) {
    const swarm = selectedSwarm;
    if (!swarm) return null;
    if (kind === 'page')
      return number >= 1 && number <= (swarm.newest_wiki_page_number ?? 0)
        ? `#wiki/w${number}`
        : null;
    const post = text.parentElement?.closest('[data-post-number]');
    const newest = post
      ? Number(post.dataset.postNumber) - 1
      : swarm.newest_post_sequence;
    return newest != null && number <= newest ? `#post/${number}` : null;
  }

  function describeReference(kind, number) {
    const key = `${kind}:${number}`;
    if (!references.has(key))
      references.set(
        key,
        (kind === 'post' ? describePost(number) : describePage(number)).catch(
          () => {
            references.delete(key);
            return t('swarm.references.unavailable', {
              reference: kind === 'post' ? `#${number}` : `w${number}`,
            });
          },
        ),
      );
    return references.get(key);
  }

  const opening = (text) => {
    const value = String(text ?? '')
      .replace(/\s+/g, ' ')
      .trim();
    return value.length > 200 ? `${value.slice(0, 199)}…` : value;
  };

  async function describePost(number) {
    const post =
      board.find((item) => item.sequence === number) ??
      page(
        await call('board.read', {
          swarm_id: selectedSwarm.id,
          message_id: `#${number}`,
        }),
      )[0];
    return {
      title: `#${number} · ${post.author?.name ?? t('swarm.participant')}`,
      text: opening(post.text),
    };
  }

  async function describePage(number) {
    const result = await call('wiki', {
      swarm_id: selectedSwarm.id,
      action: 'read',
      page_id: `w${number}`,
      limit: 300,
    });
    return {
      title: result.deleted
        ? t('swarm.references.deletedPage', {
            reference: `w${number}`,
            title: result.title,
          })
        : `w${number} · ${result.title}`,
      text: opening(result.content),
    };
  }

  // Show a cited post on the Board: open its discussion and load earlier
  // pages until it appears.
  async function revealPost(number) {
    const swarm = selectedSwarm;
    if (!swarm) return;
    const request = ++revealRequest;
    const current = () =>
      !disposed && request === revealRequest && selectedSwarm?.id === swarm.id;
    // A step to the Board, which loads only what the post needs below.
    activeTab = 'board';
    syncRoute({ step: true });
    if (number === swarm.goal_post_sequence) {
      await tick();
      const goal = document.querySelector('.swarm-goal-post');
      if (goal && current()) {
        goal.open = true;
        revealElement(goal);
      }
      return;
    }
    const [post] = page(
      await call('board.read', {
        swarm_id: swarm.id,
        message_id: `#${number}`,
      }),
    );
    if (!post || !current()) return;
    const discussionId = post.discussion_id;
    if (selectedDiscussion !== discussionId || !board.length) {
      if (!discussions.length) await loadDiscussions(swarm);
      while (
        !discussions.some((item) => item.id === discussionId) &&
        discussionCursor &&
        current()
      ) {
        const cursor = discussionCursor;
        await loadDiscussions(swarm, cursor);
        if (discussionCursor === cursor) break;
      }
      if (!current()) return;
      if (!discussions.some((item) => item.id === discussionId))
        discussions = [...discussions, { id: discussionId }];
      await chooseDiscussion(discussionId);
    }
    while (
      current() &&
      selectedDiscussion === discussionId &&
      boardCursor &&
      !board.some((item) => item.sequence === number)
    ) {
      const cursor = boardCursor;
      await loadBoard(swarm, discussionId, cursor);
      if (boardCursor === cursor) break;
    }
    if (!current()) return;
    await tick();
    revealElement(
      document.querySelector(`.board > li[data-post-number="${number}"]`),
    );
  }

  function revealElement(element) {
    if (!element) return;
    const reduced = window.matchMedia?.(
      '(prefers-reduced-motion: reduce)',
    )?.matches;
    element.scrollIntoView?.({
      block: 'center',
      behavior: reduced ? 'auto' : 'smooth',
    });
    element.tabIndex = -1;
    element.focus({ preventScroll: true });
    element.dataset.revealed = '';
    setTimeout(() => delete element.dataset.revealed, 2000);
  }

  function openContentLink(event) {
    const link = event.target.closest('a[href]');
    if (!link) return;
    event.preventDefault();
    const post = /^#post\/(\d+)$/.exec(link.getAttribute('href') ?? '');
    if (post) {
      void Promise.resolve(navigate(() => revealPost(Number(post[1])))).catch(
        (cause) => {
          if (!disposed) error = cause.message;
        },
      );
      return;
    }
    const wiki = /^#wiki\/([^/]+)$/.exec(link.getAttribute('href') ?? '');
    if (wiki) {
      void navigate(async () => {
        openTab('wiki');
        await tick();
        await host.wiki?.openPage(wiki[1]);
      });
      return;
    }
    const url = link.href;
    if (!url) return;
    const isMedia =
      Boolean(link.querySelector('img, video, audio')) ||
      /\.(avif|gif|jpe?g|mp3|mp4|ogg|png|svg|webm)(?:$|[?#])/i.test(url);
    void (isMedia ? client.openMedia(url) : client.openLink(url)).catch(
      (cause) => (error = cause.message),
    );
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
      clearTimeout(usageTimer);
      selectionRequest += 1;
      host.activity.destroy();
      clearTimeout(startupTimeout);
      offContext();
      offInvalidation();
      client.dispose();
    };
  });
  return {
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
    get board() {
      return board;
    },
    get boardCursor() {
      return boardCursor;
    },
    get selectedDiscussion() {
      return selectedDiscussion;
    },
    get discussionCursor() {
      return discussionCursor;
    },
    get profilesCursor() {
      return profilesCursor;
    },
    get swarmsCursor() {
      return swarmsCursor;
    },
    get usageLoading() {
      return usageLoading;
    },
    get usage() {
      return usage;
    },
    get activeTab() {
      return activeTab;
    },
    openTab,
    get editor() {
      return editor;
    },
    get deleteCandidate() {
      return deleteCandidate;
    },
    set deleteCandidate(value) {
      deleteCandidate = value;
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
    get postText() {
      return postText;
    },
    set postText(value) {
      postText = value;
    },
    get composeOpen() {
      return composeOpen;
    },
    set composeOpen(value) {
      composeOpen = value;
    },
    get postRecipients() {
      return postRecipients;
    },
    set postRecipients(value) {
      postRecipients = value;
    },
    get replyTo() {
      return replyTo;
    },
    set replyTo(value) {
      replyTo = value;
    },
    get posting() {
      return posting;
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
    get discussionOptions() {
      return discussionOptions;
    },
    get discussionParticipants() {
      return discussionParticipants;
    },
    get settingChanges() {
      return settingChanges;
    },
    get usageRows() {
      return usageRows;
    },
    refresh,
    openSwarm,
    loadMoreProfiles,
    loadMoreSwarms,
    loadDiscussions,
    loadBoard,
    chooseDiscussion,
    openDiscussion,
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
    post,
    contentLinks,
  };
}
