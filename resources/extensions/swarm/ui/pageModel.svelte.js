import { SvelteDate, SvelteMap } from 'svelte/reactivity';
import { t, activeLocaleTag, init } from '../../../../webui/src/lib/i18n.js';
import {
  resumableParticipantState,
  page,
  requestId,
} from './pagePresentation.js';
import { setApplicationTimeZone } from '../../../../webui/src/lib/dateTimePrefs.svelte.js';
import { onMount } from 'svelte';
import { createExtensionPageClient } from '$lib/extensionPageClient.js';

export function createSwarmPageModel(host) {
  let client = $state(null);

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

  let eventsCursor = $state(null);

  let events = $state([]);

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

  let pending = $state('');

  let context = $state({ locale: 'en', timezone: 'UTC', theme: {} });

  let profileEditor = $state(null);

  let editorKey = $state(0);

  let selectionRequest = 0;

  let overviewRequest = 0;

  let boardRequest = 0;

  let discussionsRequest = 0;

  let usageRequest = 0;

  let eventsRequest = 0;

  let directoryRequest = 0;

  let disposed = false;

  function navigate(action) {
    if (profileEditor) return profileEditor.requestTransition(action);
    return action();
  }

  function newSwarm() {
    return navigate(() => {
      selectionRequest += 1;
      editor = null;
      selectedSwarm = null;
      host.activity.leaveActivity();
      client.replaceRoute('');
      void selectRunProfile(selectedProfile);
    });
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
      label: t('swarm.runs.active', 'Active runs'),
      entries: swarms.filter((swarm) =>
        ['preparing', 'running', 'stopping'].includes(swarm.state),
      ),
    },
    {
      id: 'inactive',
      label: t('swarm.runs.inactive', 'Inactive runs'),
      entries: swarms.filter(
        (swarm) => !['preparing', 'running', 'stopping'].includes(swarm.state),
      ),
    },
  ]);

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
    { id: 'board', label: t('swarm.tabs.board', 'Board') },
    { id: 'participants', label: t('swarm.tabs.activity', 'Activity') },
    { id: 'usage', label: t('swarm.tabs.usage', 'Usage') },
    { id: 'audit', label: t('swarm.tabs.audit', 'Delivery audit') },
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
              route: t('swarm.communication.advanced', 'Advanced'),
              field,
              before: String(selectedSwarm.delivery?.[field]),
              after: String(deliveryDraft[field]),
            })),
        ]
      : [],
  );

  const usageRows = $derived(
    participantUsage.flatMap(({ participant, report }) => {
      const models = report?.usage?.usage?.models ?? [];
      return (models.length ? models : [null]).map((model, index) => ({
        id: `${participant.id}:${index}`,
        participant: participant.display_name,
        model,
        modelName: model
          ? `${model.provider}/${model.model}`
          : participant.model,
        participantRows: index === 0 ? Math.max(models.length, 1) : 0,
        toolCalls: report?.usage?.tools?.total_calls,
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

  async function refresh({ keepSelection = true } = {}) {
    const request = ++overviewRequest;
    const selection = selectionRequest;
    // Invalidation must not unmount an active form or Run inspection.
    loading = loading && !editor;
    error = '';
    try {
      const [nextProfiles, nextSwarms] = await Promise.all([
        call('profiles.list', { limit: 100 }),
        call('swarms.list', { limit: 100 }),
      ]);
      if (disposed || request !== overviewRequest) return;
      profiles = page(nextProfiles);
      swarms = page(nextSwarms);
      profilesCursor = nextProfiles.cursor ?? null;
      swarmsCursor = nextSwarms.cursor ?? null;
      const previousProfile = selectedProfile;
      if (!keepSelection || !selectedProfile)
        selectedProfile = profiles[0] ?? null;
      if (selectedProfile)
        selectedProfile =
          profiles.find((item) => item.id === selectedProfile.id) ??
          profiles[0] ??
          null;
      if (
        previousProfile?.id !== selectedProfile?.id ||
        (!editor &&
          !selectedSwarm &&
          runDirectory === defaultDirectory &&
          JSON.stringify(previousProfile?.working_directory) !==
            JSON.stringify(selectedProfile?.working_directory))
      )
        void selectRunProfile(selectedProfile);
      if (
        selection === selectionRequest &&
        selectedSwarm &&
        pending !== 'delete'
      )
        await selectSwarm(selectedSwarm.id, { silent: true });
    } catch (cause) {
      if (disposed || request !== overviewRequest) return;
      error =
        cause.message ?? t('swarm.loadError', 'The Swarm page could not load.');
    } finally {
      if (!disposed && request === overviewRequest) loading = false;
    }
  }

  async function selectSwarm(id, { silent = false } = {}) {
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
        selectedDiscussion = swarm.main_discussion_id;
        discussions = [];
        board = [];
        boardCursor = null;
        events = [];
        usage = null;
        participantUsage = [];
      }
      selectedSwarm = swarm;
      if (!silent) editor = null;
      const inspected = swarm.participants?.find(
        (item) => item.id === host.activity.history?.participant.id,
      );
      if (silent && inspected) {
        void host.activity.inspectParticipant(inspected, {
          activate: false,
          preserve:
            inspected.lifecycle_run_id ===
            host.activity.history.participant.lifecycle_run_id,
        });
      }
      await loadDiscussions(swarm);
      if (disposed || request !== selectionRequest) return;
      await Promise.all([
        loadBoard(swarm, selectedDiscussion),
        loadEvents(swarm),
        loadUsage(swarm),
      ]);
      if (!disposed && request === selectionRequest && !silent)
        await client.replaceRoute(`/swarms/${id}`);
    } catch (cause) {
      if (!disposed && request === selectionRequest) error = cause.message;
    }
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

  async function loadBoard(
    swarm = selectedSwarm,
    discussionId = selectedDiscussion,
    cursor = null,
  ) {
    if (!swarm) return;
    const selection = selectionRequest;
    const request = ++boardRequest;
    const result = await call('board.read', {
      swarm_id: swarm.id,
      discussion_id: discussionId,
      limit: 100,
      ...(cursor ? { cursor } : {}),
    });
    if (
      disposed ||
      selection !== selectionRequest ||
      request !== boardRequest ||
      selectedSwarm?.id !== swarm.id ||
      selectedDiscussion !== discussionId
    )
      return;
    const posts = [...page(result)].reverse();
    board = cursor ? [...board, ...posts] : posts;
    boardCursor = result.next_cursor ?? result.cursor ?? null;
  }

  async function chooseDiscussion(id) {
    selectedDiscussion = id;
    board = [];
    boardCursor = null;
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

  async function loadEvents(swarm = selectedSwarm) {
    if (swarm) {
      const selection = selectionRequest;
      const request = ++eventsRequest;
      const result = await call('swarms.events', {
        swarm_id: swarm.id,
        limit: 100,
      });
      if (
        disposed ||
        selection !== selectionRequest ||
        request !== eventsRequest ||
        selectedSwarm?.id !== swarm.id
      )
        return;
      events = page(result);
      eventsCursor = result.cursor ?? null;
    }
  }

  async function loadMoreEvents() {
    if (!selectedSwarm || !eventsCursor) return;
    const selection = selectionRequest;
    const request = ++eventsRequest;
    const result = await call('swarms.events', {
      swarm_id: selectedSwarm.id,
      limit: 100,
      cursor: eventsCursor,
    });
    if (disposed || selection !== selectionRequest || request !== eventsRequest)
      return;
    events = [...events, ...page(result)];
    eventsCursor = result.cursor ?? null;
  }

  async function loadUsage(swarm = selectedSwarm) {
    if (!swarm) return;
    const selection = selectionRequest;
    const request = ++usageRequest;
    const reports = await Promise.all([
      call('swarms.usage', { swarm_id: swarm.id }),
      ...(swarm.participants ?? []).map(async (participant) => ({
        participant,
        report: await call('swarms.usage', {
          swarm_id: swarm.id,
          participant_id: participant.id,
        }),
      })),
    ]);
    if (
      disposed ||
      selection !== selectionRequest ||
      request !== usageRequest ||
      selectedSwarm?.id !== swarm.id
    )
      return;
    usage = reports[0];
    participantUsage = reports.slice(1);
  }

  async function saveProfile(profile) {
    const saved = await call('profiles.save', {
      profile,
      expected_revision: profile.revision || null,
    });
    if (!profile.id) {
      profiles = [...profiles, saved.profile];
      selectedProfile = saved.profile;
      editor = saved.profile;
    } else {
      profiles = profiles.map((item) =>
        item.id === saved.profile.id ? saved.profile : item,
      );
    }
    selectedProfile = saved.profile;
    return saved.profile;
  }

  async function openProfile(profile = 'new') {
    if (pending === 'profile') return;
    pending = 'profile';
    error = '';
    try {
      catalog = (await call('catalog'))?.catalog ?? {};
      selectionRequest += 1;
      host.activity.leaveActivity();
      selectedSwarm = null;
      if (profile !== 'new') selectedProfile = profile;
      editorKey += 1;
      editor = profile;
    } catch (cause) {
      error = cause.message;
    } finally {
      pending = '';
    }
  }

  async function deleteProfile() {
    const profile = deleteCandidate;
    if (!profile) return;
    try {
      await call('profiles.delete', {
        profile_id: profile.id,
        expected_revision: profile.revision,
      });
      if (selectedProfile?.id === profile.id) selectedProfile = null;
      deleteCandidate = null;
      await refresh();
    } catch (cause) {
      error = cause.message;
    }
  }

  async function deleteSwarm() {
    const candidate = swarmDeleteCandidate;
    if (!candidate || pending) return;
    pending = 'delete';
    deleteError = '';
    try {
      await call('swarms.delete', { swarm_id: candidate.id });
      if (selectedSwarm?.id === candidate.id) newSwarm();
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
      error = t('swarm.start.validation', 'Choose a Swarm and enter a goal.');
      return;
    }
    if (directoryLoading || !runDirectory.trim()) {
      error = t('swarm.start.directoryRequired', 'Choose a working directory.');
      return;
    }
    pending = 'start';
    try {
      const result = await call('swarms.start', {
        profile_id: selectedProfile.id,
        expected_profile_revision: selectedProfile.revision,
        prompt: goal,
        request_id: requestId(),
        ...(runDirectory !== defaultDirectory
          ? { working_directory: runDirectory }
          : {}),
      });
      goal = '';
      await refresh();
      await selectSwarm(result.swarm_id ?? result.id);
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
      await call(`swarms.${operation}`, {
        swarm_id: selectedSwarm.id,
        ...(participantId ? { participant_id: participantId } : {}),
        request_id: requestId(),
      });
      await refresh();
    } catch (cause) {
      error = cause.message;
    } finally {
      pending = '';
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
      await loadEvents();
    } catch (cause) {
      error = cause.message;
    } finally {
      pending = '';
    }
  }

  async function post() {
    if (!selectedSwarm || !postText.trim()) return;
    posting = true;
    error = '';
    try {
      await call('board.post', {
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
        request_id: requestId(),
      });
      postText = '';
      postRecipients = '';
      replyTo = '';
      composeOpen = false;
      await loadBoard();
      await loadEvents();
    } catch (cause) {
      error = cause.message;
    } finally {
      posting = false;
    }
  }

  function contentLinks(node) {
    node.addEventListener('click', openContentLink);
    return {
      destroy: () => node.removeEventListener('click', openContentLink),
    };
  }

  function openContentLink(event) {
    const link = event.target.closest('a[href]');
    if (!link || !client) return;
    event.preventDefault();
    const url = link.href;
    if (!url) return;
    const isMedia =
      Boolean(link.querySelector('img, video, audio')) ||
      /\.(avif|gif|jpe?g|mp3|mp4|ogg|png|svg|webm)(?:$|[?#])/i.test(url);
    void (isMedia ? client.openMedia(url) : client.openLink(url)).catch(
      (cause) => (error = cause.message),
    );
  }

  function routeSelection(route) {
    const match = /^\/swarms\/([^/]+)$/.exec(route ?? '');
    if (match && selectedSwarm?.id !== match[1]) selectSwarm(match[1]);
  }

  onMount(() => {
    client ??= host.bridgeClient ?? createExtensionPageClient();
    const startupTimeout = setTimeout(() => {
      loading = false;
      error = t(
        'swarm.hostUnavailable',
        'The Swarm page could not connect. Reopen Swarms to try again.',
      );
    }, 10_000);
    let initialized = false;
    const offContext = client.onContext((next) => {
      clearTimeout(startupTimeout);
      const previousRoute = context?.route;
      applyContext(next);
      if (!initialized) {
        initialized = true;
        routeSelection(next.route);
        refresh();
      } else if (next.route !== previousRoute) routeSelection(next.route);
    });
    const offInvalidation = client.onInvalidation(() => refresh());
    return () => {
      disposed = true;
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
    get eventsCursor() {
      return eventsCursor;
    },
    get events() {
      return events;
    },
    get usage() {
      return usage;
    },
    get activeTab() {
      return activeTab;
    },
    set activeTab(value) {
      activeTab = value;
    },
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
    get settingChanges() {
      return settingChanges;
    },
    get usageRows() {
      return usageRows;
    },
    refresh,
    selectSwarm,
    loadMoreProfiles,
    loadMoreSwarms,
    loadDiscussions,
    loadBoard,
    chooseDiscussion,
    openDiscussion,
    loadMoreEvents,
    saveProfile,
    openProfile,
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
