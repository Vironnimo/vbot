<script>
  import { onMount } from 'svelte';
  import { createExtensionPageClient } from '$lib/extensionPageClient.js';
  import ChatAssistantRun from '../../../../webui/src/components/chat/ChatAssistantRun.svelte';
  import ChatTimelineEntry from '../../../../webui/src/components/chat/ChatTimelineEntry.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import FormField from '../../../../webui/src/components/ui/FormField.svelte';
  import TabList from '../../../../webui/src/components/ui/TabList.svelte';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import EmptyState from '../../../../webui/src/components/ui/EmptyState.svelte';
  import StatusChip from '../../../../webui/src/components/ui/StatusChip.svelte';
  import { formatTokenUsageTooltip } from '../../../../webui/src/lib/tokenUsageTooltip.js';
  import { setApplicationTimeZone } from '../../../../webui/src/lib/dateTimePrefs.svelte.js';
  import { tooltip } from '../../../../webui/src/lib/tooltip.js';
  import { init, t, activeLocaleTag } from '../../../../webui/src/lib/i18n.js';
  import { visibleTimelineItemsForRender } from '../../../../webui/src/lib/chatTimeline.js';
  import ProfileEditor from './ProfileEditor.svelte';

  let { bridgeClient = null } = $props();
  let client = $state(null),
    loading = $state(true),
    error = $state(''),
    profiles = $state([]),
    swarms = $state([]),
    catalog = $state({});
  let selectedProfile = $state(null),
    selectedSwarm = $state(null),
    board = $state([]),
    boardCursor = $state(null),
    discussions = $state([]),
    selectedDiscussion = $state(''),
    discussionCursor = $state(null),
    profilesCursor = $state(null),
    swarmsCursor = $state(null),
    eventsCursor = $state(null);
  let events = $state([]),
    usage = $state(null),
    participantUsage = $state([]),
    activeTab = $state('board'),
    editor = $state(null),
    deleteCandidate = $state(null),
    swarmDeleteCandidate = $state(null),
    deleteError = $state(''),
    settingsOpen = $state(false),
    deliveryDraft = $state(null),
    profileSnapshotOpen = $state(false);
  let goal = $state(''),
    postText = $state(''),
    composeOpen = $state(false),
    postRecipients = $state(''),
    replyTo = $state(''),
    posting = $state(false),
    pending = $state(''),
    history = $state(null),
    live = $state([]),
    context = $state({ locale: 'en', timezone: 'UTC', theme: {} });
  let profileEditor = $state(null);
  let editorKey = $state(0);
  let currentSubscription = null;
  let activityRequest = 0;
  let selectionRequest = 0;
  function leaveActivity() {
    activityRequest += 1;
    currentSubscription?.();
    currentSubscription = null;
    history = null;
    live = [];
  }
  function navigate(action) {
    if (profileEditor) return profileEditor.requestTransition(action);
    return action();
  }
  function newSwarm() {
    return navigate(() => {
      selectionRequest += 1;
      editor = null;
      selectedSwarm = null;
      leaveActivity();
      client.replaceRoute('');
    });
  }
  const requestId = () =>
    crypto.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
  function participantColor(id) {
    let hash = 2166136261;
    for (const char of id ?? '')
      hash = Math.imul(hash ^ char.codePointAt(0), 16777619);
    return `hsl(${(hash >>> 0) % 360} 60% 75%)`;
  }
  function participantInitials(name) {
    const parts = (name ?? '').trim().split(/\s+/u).filter(Boolean);
    if (!parts.length) return '?';
    const last = parts.at(-1);
    if (parts.length > 1 && /^\d+$/u.test(last))
      return `${Array.from(parts[0])[0]}${last}`.toUpperCase();
    return (
      parts.length > 1
        ? `${Array.from(parts[0])[0]}${Array.from(last)[0]}`
        : Array.from(parts[0]).slice(0, 2).join('')
    ).toUpperCase();
  }
  const page = (value) =>
    Array.isArray(value) ? value : (value?.entries ?? value?.items ?? []);
  const call = (operation, arguments_ = {}) =>
    client.operation(operation, arguments_);
  const isActive = (state) =>
    ['preparing', 'running', 'idle', 'needs_attention', 'stopping'].includes(
      state,
    );
  const resumableParticipantState = (state) =>
    ['idle', 'failed', 'cancelled', 'interrupted'].includes(state);
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
        }).format(new Date(value))
      : '';
  const discussionOptions = $derived(discussions);
  function usageCount(value) {
    if (!Number.isFinite(value))
      return t('swarm.usage.unavailable', 'Unavailable');
    const units = [
      [1e9, t('swarm.usage.billion', 'mrd')],
      [1e6, t('swarm.usage.million', 'mio')],
      [1e3, t('swarm.usage.thousand', 'k')],
    ];
    const [scale, suffix] = units.find(
      ([scale]) => Math.abs(value) >= scale,
    ) ?? [1, ''];
    const number = new Intl.NumberFormat(activeLocaleTag(), {
      maximumFractionDigits: scale === 1 ? 0 : 1,
    }).format(value / scale);
    return suffix ? `${number} ${suffix}` : number;
  }
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

  const activityTimeline = $derived(
    history
      ? visibleTimelineItemsForRender({
          messages: history.data.messages ?? [],
          runEvents: live,
          streamingRunEvents: [],
          status: history.data.status ?? 'completed',
        })
      : [],
  );
  const selectedParticipant = $derived(
    selectedSwarm?.participants?.find(
      (item) => item.id === history?.participant.id,
    ),
  );
  const contextWindow = $derived(
    catalog.models?.find((model) => model.id === history?.participant.model)
      ?.context_window,
  );
  const contextRatio = $derived(
    Number.isFinite(contextWindow) &&
      contextWindow > 0 &&
      Number.isFinite(history?.data.context_usage?.tokens)
      ? Math.min(1, history.data.context_usage.tokens / contextWindow)
      : 0,
  );
  const contextTokens = $derived(
    Number.isFinite(history?.data.context_usage?.tokens)
      ? `${history.data.context_usage.estimated ? '~' : ''}${new Intl.NumberFormat(activeLocaleTag()).format(history.data.context_usage.tokens)}${contextWindow ? ` / ${new Intl.NumberFormat(activeLocaleTag()).format(contextWindow)}` : ''}`
      : t('swarm.usage.unavailable', 'Unavailable'),
  );
  const activityContextTooltip = $derived(
    formatTokenUsageTooltip(
      history?.data.context_usage,
      history?.data.usage,
      history?.data.session_usage,
      contextWindow,
    ),
  );
  function tokensUsed(counts) {
    return usageCount(
      counts?.measured_input_tokens +
        counts?.measured_output_tokens +
        counts?.estimated_input_tokens +
        counts?.estimated_output_tokens,
    );
  }
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
    // Invalidation must not unmount an active form or Run inspection.
    loading = loading && !editor;
    error = '';
    try {
      const [nextProfiles, nextSwarms] = await Promise.all([
        call('profiles.list', { limit: 100 }),
        call('swarms.list', { limit: 100 }),
      ]);
      profiles = page(nextProfiles);
      swarms = page(nextSwarms);
      profilesCursor = nextProfiles.cursor ?? null;
      swarmsCursor = nextSwarms.cursor ?? null;
      if (!keepSelection || !selectedProfile)
        selectedProfile = profiles[0] ?? null;
      if (selectedProfile)
        selectedProfile =
          profiles.find((item) => item.id === selectedProfile.id) ??
          profiles[0] ??
          null;
      if (selectedSwarm && pending !== 'delete')
        await selectSwarm(selectedSwarm.id, { silent: true });
    } catch (cause) {
      error =
        cause.message ?? t('swarm.loadError', 'The Swarm page could not load.');
    } finally {
      loading = false;
    }
  }
  async function selectSwarm(id, { silent = false } = {}) {
    const request = ++selectionRequest;
    if (!silent) {
      error = '';
      leaveActivity();
      composeOpen = false;
    }
    try {
      const preservedDiscussion =
        selectedSwarm?.id === id ? selectedDiscussion : '';
      const swarm = (await call('swarms.get', { swarm_id: id })).swarm;
      if (request !== selectionRequest) return;
      selectedSwarm = swarm;
      if (!silent) editor = null;
      const inspected = swarm.participants?.find(
        (item) => item.id === history?.participant.id,
      );
      if (
        silent &&
        inspected &&
        inspected.lifecycle_run_id !== history.participant.lifecycle_run_id
      ) {
        void inspectParticipant(inspected, { activate: false });
      }
      await loadDiscussions(swarm);
      const nextDiscussion = discussions.some(
        (item) => item.id === preservedDiscussion,
      )
        ? preservedDiscussion
        : swarm.main_discussion_id;
      selectedDiscussion = nextDiscussion;
      await Promise.all([
        loadBoard(swarm, nextDiscussion),
        loadEvents(swarm),
        loadUsage(swarm),
      ]);
      if (!silent) await client.replaceRoute(`/swarms/${id}`);
    } catch (cause) {
      error = cause.message;
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
    const result = await call('board.list', {
      swarm_id: swarm.id,
      limit: 100,
      ...(cursor ? { cursor } : {}),
    });
    discussions = cursor ? [...discussions, ...page(result)] : page(result);
    discussionCursor = result.cursor ?? null;
  }
  async function loadBoard(
    swarm = selectedSwarm,
    discussionId = selectedDiscussion,
    cursor = null,
  ) {
    if (!swarm) return;
    const result = await call('board.read', {
      swarm_id: swarm.id,
      discussion_id: discussionId,
      limit: 100,
      ...(cursor ? { cursor } : {}),
    });
    const posts = [...page(result)].reverse();
    board = cursor ? [...board, ...posts] : posts;
    boardCursor = result.next_cursor ?? result.cursor ?? null;
  }
  async function chooseDiscussion(id) {
    selectedDiscussion = id;
    await loadBoard(selectedSwarm, id);
  }
  async function loadEvents(swarm = selectedSwarm) {
    if (swarm) {
      const result = await call('swarms.events', {
        swarm_id: swarm.id,
        limit: 100,
      });
      events = page(result);
      eventsCursor = result.cursor ?? null;
    }
  }
  async function loadMoreEvents() {
    if (!selectedSwarm || !eventsCursor) return;
    const result = await call('swarms.events', {
      swarm_id: selectedSwarm.id,
      limit: 100,
      cursor: eventsCursor,
    });
    events = [...events, ...page(result)];
    eventsCursor = result.cursor ?? null;
  }
  async function loadUsage(swarm = selectedSwarm) {
    if (!swarm) return;
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
      leaveActivity();
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
      error = t('swarm.start.validation', 'Choose a profile and enter a goal.');
      return;
    }
    pending = 'start';
    try {
      const result = await call('swarms.start', {
        profile_id: selectedProfile.id,
        expected_profile_revision: selectedProfile.revision,
        prompt: goal,
        request_id: requestId(),
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
  function activityLinks(node) {
    node.addEventListener('click', openActivityLink);
    return {
      destroy: () => node.removeEventListener('click', openActivityLink),
    };
  }

  function openActivityLink(event) {
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

  async function reconcileActivity(
    request,
    swarmId,
    participant,
    settled = false,
  ) {
    try {
      const data = await client.readHistory(swarmId, participant.id, {
        limit: 100,
      });
      if (request === activityRequest) {
        history = { participant, data };
        if (settled) live = [];
      }
    } catch (cause) {
      if (request === activityRequest) error = cause.message;
    }
  }
  async function inspectParticipant(participant, { activate = true } = {}) {
    if (!selectedSwarm) return;
    if (activate) activeTab = 'participants';
    leaveActivity();
    const request = activityRequest;
    const swarmId = selectedSwarm.id;
    await Promise.all([
      reconcileActivity(request, swarmId, participant),
      catalog.models
        ? Promise.resolve()
        : call('catalog')
            .then((result) => {
              catalog = result.catalog;
            })
            .catch((cause) => {
              if (request === activityRequest) error = cause.message;
            }),
    ]);
    if (
      request !== activityRequest ||
      !history ||
      !participant.lifecycle_run_id
    )
      return;
    let key = null;
    const buffered = [];
    const receive = (id, event) => {
      if (request !== activityRequest || id !== key) return;
      live = [...live, event];
      const payload = event.payload ?? {};
      if (
        history &&
        (payload.context_usage || payload.session_usage || payload.usage)
      ) {
        history = {
          ...history,
          data: {
            ...history.data,
            ...(payload.context_usage
              ? { context_usage: payload.context_usage }
              : {}),
            ...(payload.session_usage
              ? { session_usage: payload.session_usage }
              : {}),
            ...(payload.usage ? { usage: payload.usage } : {}),
          },
        };
      }
      if (
        [
          'run_completed',
          'run_cancelled',
          'run_failed',
          'run_interrupted',
        ].includes(event.type)
      ) {
        void reconcileActivity(request, swarmId, participant, true);
      }
    };
    const off = client.onRunEvent((id, event) => {
      if (key === null) buffered.push([id, event]);
      else receive(id, event);
    });
    currentSubscription = () => {
      off();
      if (key) client.unsubscribeRun(key);
    };
    try {
      const subscription = await client.subscribeRun(
        swarmId,
        participant.lifecycle_run_id,
      );
      key = subscription.subscription_id;
      if (request !== activityRequest) {
        off();
        client.unsubscribeRun(key);
        return;
      }
      for (const [id, event] of buffered) receive(id, event);
    } catch (cause) {
      off();
      if (request === activityRequest) error = cause.message;
    }
  }
  function routeSelection(route) {
    const match = /^\/swarms\/([^/]+)$/.exec(route ?? '');
    if (match && selectedSwarm?.id !== match[1]) selectSwarm(match[1]);
  }
  onMount(() => {
    client ??= bridgeClient ?? createExtensionPageClient();
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
      clearTimeout(startupTimeout);
      currentSubscription?.();
      offContext();
      offInvalidation();
      client.dispose();
    };
  });
</script>

{#snippet participantAvatar(id, name, kind = 'participant')}
  <span
    class="participant-avatar"
    style:--participant-color={kind === 'participant' && id
      ? participantColor(id)
      : 'var(--text-med)'}
    aria-hidden="true">{participantInitials(name)}</span
  >
{/snippet}

<svelte:head><title>{t('swarm.title', 'Swarms')}</title></svelte:head>
{#snippet actionIcon(kind)}
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.7"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    {#if kind === 'plus'}<path d="M12 5v14M5 12h14" />
    {:else if kind === 'close'}<path d="m6 6 12 12M18 6 6 18" />
    {:else if kind === 'refresh'}<path
        d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2"
      />
    {:else if kind === 'play'}<path d="m8 5 11 7-11 7Z" />
    {:else if kind === 'stop'}<rect x="6" y="6" width="12" height="12" rx="1" />
    {:else if kind === 'edit'}<path
        d="m14 5 5 5M4 20l5-1L20 8a2 2 0 0 0-5-5L4 14Z"
      />
    {:else if kind === 'trash'}<path
        d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7M14 10v7"
      />
    {:else if kind === 'settings'}<path d="M4 7h16M4 17h16" /><circle
        cx="9"
        cy="7"
        r="3"
        fill="var(--bg)"
      /><circle cx="15" cy="17" r="3" fill="var(--bg)" />
    {:else}<path d="M6 3h9l4 4v14H6ZM14 3v5h5M9 12h7M9 16h7" />{/if}
  </svg>
{/snippet}
<main class="swarm-page">
  <aside class="secondary-pane">
    <div class="secondary-pane__header">
      <span class="secondary-pane__title"
        >{t('swarm.profiles', 'Profiles')}</span
      >
      <Button
        variant="tertiary"
        icon
        ariaLabel={t('swarm.newProfile', 'New profile')}
        tooltip={t('swarm.newProfile', 'New profile')}
        loading={pending === 'profile'}
        onClick={() => navigate(() => openProfile())}
        >{@render actionIcon('plus')}</Button
      >
    </div>
    <div class="secondary-pane__scroll">
      <nav class="secondary-list" aria-label={t('swarm.profiles', 'Profiles')}>
        {#each profiles as profile (profile.id)}
          <button
            class="secondary-list__item"
            class:active={editor?.id === profile.id}
            aria-current={editor?.id === profile.id ? 'page' : undefined}
            onclick={() => navigate(() => openProfile(profile))}
          >
            <strong>{profile.name}</strong><span
              >{t('swarm.profile.participantSummary', '{count} participants', {
                count: (profile.participants ?? []).reduce(
                  (sum, row) => sum + row.count,
                  0,
                ),
              })}</span
            >
          </button>
        {:else}<EmptyState
            density="compact"
            title={t('swarm.noProfiles', 'No profiles yet.')}
            description={t(
              'swarm.noProfilesHelp',
              'Create a profile to start a Swarm.',
            )}
          />{/each}
        {#if profilesCursor}<Button
            variant="tertiary"
            onClick={loadMoreProfiles}
            >{t('swarm.profiles.more', 'Load more profiles')}</Button
          >{/if}
      </nav>
      <div class="secondary-pane__header swarms-head">
        <span class="secondary-pane__title"
          >{t('swarm.retained', 'Retained Swarms')}</span
        >
        <Button
          variant="tertiary"
          icon
          ariaLabel={t('swarm.newSwarm', 'New Swarm')}
          tooltip={t('swarm.newSwarm', 'New Swarm')}
          onClick={newSwarm}>{@render actionIcon('plus')}</Button
        >
      </div>
      <nav
        class="secondary-list"
        aria-label={t('swarm.retained', 'Retained Swarms')}
      >
        {#each swarms as swarm (swarm.id)}
          <button
            class="secondary-list__item"
            class:active={!editor && selectedSwarm?.id === swarm.id}
            use:tooltip={swarm.title || swarm.id}
            onclick={() => navigate(() => selectSwarm(swarm.id))}
          >
            <strong
              >{swarm.title ||
                swarm.prompt?.split(/\r?\n/)[0] ||
                swarm.id}</strong
            >
            <span
              ><i class="dot" class:running={swarm.state === 'running'}></i>{t(
                `swarm.state.${swarm.state}`,
                swarm.state,
              )} · {swarm.participant_count}</span
            >
          </button>
        {/each}
        {#if swarmsCursor}<Button variant="tertiary" onClick={loadMoreSwarms}
            >{t('swarm.swarms.more', 'Load more Swarms')}</Button
          >{/if}
      </nav>
    </div>
    <div class="sidebar-footer">
      <Button variant="secondary" onClick={newSwarm}
        >{@render actionIcon('play')}{t('swarm.newSwarm', 'New Swarm')}</Button
      >
    </div>
  </aside>
  <div class="workspace">
    {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
    {#if loading}<Banner variant="info" role="status"
        >{t('swarm.loading', 'Loading Swarms…')}</Banner
      >{/if}
    {#if editor}
      {#key editorKey}
        <ProfileEditor
          bind:this={profileEditor}
          profile={editor === 'new' ? null : editor}
          {catalog}
          bridgeClient={client}
          onSave={saveProfile}
          onCancel={newSwarm}
        />
      {/key}
    {:else}
      <section class="content">
        <div class="workspace-toolbar">
          <span class="eyebrow">{t('swarm.title', 'Swarms')}</span>
          <Button
            variant="tertiary"
            icon
            ariaLabel={t('common.refresh', 'Refresh')}
            tooltip={t('common.refresh', 'Refresh')}
            disabled={loading}
            onClick={() => refresh()}>{@render actionIcon('refresh')}</Button
          >
        </div>
        {#if selectedSwarm}<div class="swarm-head">
            <div>
              <h2>{t('swarm.userPrompt', 'User Prompt:')}</h2>
              <p class="goal">{selectedSwarm.prompt}</p>
              <p class="muted">
                {selectedSwarm.working_directory?.path ??
                  selectedSwarm.working_directory?.project_id ??
                  ''}
              </p>
            </div>
            <div class="actions">
              {#if isActive(selectedSwarm.state)}<Button
                  variant="danger"
                  loading={pending === 'stop'}
                  onClick={() => lifecycle('stop')}
                  >{@render actionIcon('stop')}{pending === 'stop'
                    ? t('swarm.stopping', 'Stopping...')
                    : t('swarm.stop', 'Stop')}</Button
                >{/if}{#if canResume}<Button
                  variant="primary"
                  loading={pending === 'resume'}
                  disabled={pending === 'stop'}
                  onClick={() => lifecycle('resume')}
                  >{@render actionIcon('play')}{pending === 'resume'
                    ? t('swarm.resuming', 'Resuming...')
                    : t('swarm.resume', 'Resume')}</Button
                >{/if}<Button
                variant="danger"
                disabled={!!pending || isActive(selectedSwarm.state)}
                tooltip={isActive(selectedSwarm.state)
                  ? t(
                      'swarm.deleteRun.stopFirst',
                      'Stop the Swarm before deleting it.',
                    )
                  : t('swarm.deleteRun.title', 'Delete Swarm')}
                onClick={() => {
                  deleteError = '';
                  swarmDeleteCandidate = selectedSwarm;
                }}>{t('swarm.deleteRun.title', 'Delete Swarm')}</Button
              ><Button
                variant="secondary"
                onClick={() => (profileSnapshotOpen = true)}
                icon
                ariaLabel={t(
                  'swarm.profileSnapshot',
                  'Inspect profile snapshot',
                )}
                tooltip={t('swarm.profileSnapshot', 'Inspect profile snapshot')}
                >{@render actionIcon('document')}</Button
              ><Button
                variant="tertiary"
                icon
                onClick={openDelivery}
                ariaLabel={t(
                  'swarm.changeCommunication',
                  'Change communication settings',
                )}
                tooltip={t(
                  'swarm.changeCommunication',
                  'Change communication settings',
                )}>{@render actionIcon('settings')}</Button
              >
            </div>
          </div>
          <div class="swarm-tabs">
            <TabList
              items={tabs}
              value={activeTab}
              ariaLabel={t('swarm.details', 'Swarm details')}
              onChange={(next) => (activeTab = next)}
            /><StatusChip
              variant={isActive(selectedSwarm.state) ? 'warn' : 'neutral'}
              >{t(
                `swarm.state.${selectedSwarm.state}`,
                selectedSwarm.state,
              )}</StatusChip
            >
          </div>
          {#if activeTab === 'board'}<section class="panel" role="tabpanel">
              <div class="section-head">
                <h3>{t('swarm.board.title', 'Board')}</h3>
                <Button variant="secondary" onClick={() => (composeOpen = true)}
                  >{t('swarm.board.openComposer', 'Write post')}</Button
                >
                <FormField
                  controlId="swarm-discussion"
                  label={t('swarm.board.discussion', 'Discussion')}
                  ><select
                    class="s-input"
                    id="swarm-discussion"
                    value={selectedDiscussion}
                    onchange={(event) =>
                      chooseDiscussion(event.currentTarget.value)}
                    >{#each discussionOptions as discussion (discussion.id)}<option
                        value={discussion.id}
                        >{discussion.title ??
                          discussion.name ??
                          discussion.id}</option
                      >{/each}</select
                  ></FormField
                >
                {#if discussionCursor}<Button
                    variant="secondary"
                    onClick={() =>
                      loadDiscussions(selectedSwarm, discussionCursor)}
                    >{t(
                      'swarm.board.moreDiscussions',
                      'Load more discussions',
                    )}</Button
                  >{/if}
              </div>
              <section
                class="participant-pane"
                aria-label={t('swarm.participants', 'Participants')}
              >
                <p class="eyebrow">
                  {t('swarm.participants', 'Participants')}
                </p>
                <div class="participant-row">
                  {#each selectedSwarm.participants ?? [] as participant (participant.id)}<Button
                      variant="secondary"
                      onClick={() => inspectParticipant(participant)}
                      >{@render participantAvatar(
                        participant.id,
                        participant.display_name,
                      )}<span
                        ><strong>{participant.display_name}</strong><small
                          >{participant.model} / {participant.state} / {participant.pending_count ??
                            0}
                          {t('swarm.pending', 'pending')}</small
                        ></span
                      >{#if participant.run_active}<span class="run-indicator"
                          >{t('swarm.runActive', 'Run active')}</span
                        >{/if}</Button
                    >{/each}
                </div>
              </section>
              {#if board.length === 0}<EmptyState
                  density="compact"
                  title={t('swarm.board.empty', 'No Board messages yet.')}
                />{:else}<ol class="board">
                  {#each board as post (post.id)}<li>
                      <div class="post-header">
                        <div class="post-author">
                          {@render participantAvatar(
                            post.author?.id,
                            post.author?.name,
                            post.author?.kind,
                          )}
                          <strong
                            >{post.author_name ??
                              post.author?.name ??
                              t('swarm.participant', 'Participant')}</strong
                          >
                        </div>
                        <time datetime={post.created_at}
                          >{date(post.created_at)}</time
                        >
                      </div>
                      <p>{post.text}</p>
                      {#if post.reply_to}<small
                          >{t('swarm.board.reply', 'Reply to {id}', {
                            id: post.reply_to,
                          })}</small
                        >{/if}{#if post.recipients?.length}<small
                          >{t('swarm.board.pinged', 'Pinged: {names}', {
                            names: post.recipients.join(', '),
                          })}</small
                        >{/if}
                    </li>{/each}
                </ol>
                {#if boardCursor}<Button
                    variant="secondary"
                    onClick={() =>
                      loadBoard(selectedSwarm, selectedDiscussion, boardCursor)}
                    >{t('swarm.board.more', 'Load earlier messages')}</Button
                  >{/if}{/if}
            </section>
          {:else if activeTab === 'participants'}<section
              class="panel"
              role="tabpanel"
            >
              <h3>{t('swarm.participants', 'Participants')}</h3>
              <div class="participants">
                {#each selectedSwarm.participants ?? [] as participant (participant.id)}<Button
                    variant="secondary"
                    aria-pressed={history?.participant.id === participant.id}
                    onClick={() => inspectParticipant(participant)}
                    >{@render participantAvatar(
                      participant.id,
                      participant.display_name,
                    )}<span
                      class:running={participant.state === 'running'}
                      class="dot"
                    ></span><span
                      ><strong>{participant.display_name}</strong><small
                        >{participant.model} · {participant.state} · {participant.pending_count ??
                          0}
                        {t('swarm.pending', 'pending')}</small
                      ></span
                    ></Button
                  >{/each}
              </div>
              {#if history}<article class="history" use:activityLinks>
                  <div class="section-head">
                    <h3>
                      {history.participant.display_name}
                      {t('swarm.activity', 'activity')}
                    </h3>
                    <div class="actions">
                      <button
                        class="context-usage"
                        aria-label={t(
                          'chat.contextRingLabel',
                          'Context window usage',
                        )}
                        use:tooltip={activityContextTooltip}
                      >
                        <svg
                          width="18"
                          height="18"
                          viewBox="0 0 18 18"
                          aria-hidden="true"
                          ><circle
                            cx="9"
                            cy="9"
                            r="6"
                            fill="none"
                            stroke="var(--border-2)"
                            stroke-width="2"
                          /><circle
                            cx="9"
                            cy="9"
                            r="6"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="2"
                            pathLength="1"
                            stroke-dasharray={`${contextRatio} 1`}
                            transform="rotate(-90 9 9)"
                          /></svg
                        >
                        {t('swarm.context', 'Context')}: {contextTokens}
                      </button>
                      {#if canResume && selectedParticipant && !selectedParticipant.run_active && resumableParticipantState(selectedParticipant.state)}
                        <Button
                          variant="primary"
                          disabled={Boolean(pending)}
                          onClick={() =>
                            lifecycle('resume', selectedParticipant.id)}
                          >{t(
                            'swarm.resumeParticipant',
                            'Resume participant',
                          )}</Button
                        >
                      {/if}
                      <Button
                        variant="tertiary"
                        icon
                        ariaLabel={t('common.close', 'Close')}
                        tooltip={t('common.close', 'Close')}
                        onClick={leaveActivity}
                        >{@render actionIcon('close')}</Button
                      >
                    </div>
                  </div>
                  {#each activityTimeline as item (item.id)}
                    {#if item.type === 'assistant_run'}<ChatAssistantRun
                        {item}
                        agentName={history.participant.display_name}
                      />{:else}<ChatTimelineEntry
                        {item}
                        agentName={history.participant.display_name}
                        messageEditingDisabled
                      />{/if}
                  {:else}<EmptyState
                      density="compact"
                      title={t('swarm.noActivity', 'No retained activity yet.')}
                    />{/each}
                </article>{/if}
            </section>
          {:else if activeTab === 'usage'}<section
              class="panel"
              role="tabpanel"
            >
              <h3>{t('swarm.usage', 'Usage')}</h3>
              <dl class="swarm-identity">
                <dt>{t('swarm.id', 'Swarm ID')}</dt>
                <dd>{selectedSwarm.id}</dd>
              </dl>
              {#if usage?.usage}<dl class="usage-summary">
                  <div>
                    <dt>
                      {t('swarm.usage.tokensUsed', 'Tokens used')}
                    </dt>
                    <dd>{tokensUsed(usage.usage.usage?.totals)}</dd>
                  </div>
                  <div>
                    <dt>{t('swarm.usage.toolCalls', 'Tool Calls')}</dt>
                    <dd>
                      {usageCount(usage.usage.tools?.total_calls)}
                    </dd>
                  </div>
                </dl>
                {#if usageRows.length}<div class="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>{t('swarm.usage.participant', 'Participant')}</th>
                          <th>{t('swarm.usage.model', 'Model')}</th>
                          <th>{t('swarm.usage.tokensUsed', 'Tokens used')}</th>
                          <th>{t('swarm.usage.toolCalls', 'Tool Calls')}</th>
                          <th>{t('swarm.usage.runs', 'Runs')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {#each usageRows as row (row.id)}
                          <tr>
                            {#if row.participantRows}
                              <td rowspan={row.participantRows}
                                >{row.participant}</td
                              >
                            {/if}
                            <td>{row.modelName}</td>
                            <td>{tokensUsed(row.model)}</td>
                            {#if row.participantRows}
                              <td rowspan={row.participantRows}
                                >{usageCount(row.toolCalls)}</td
                              >
                            {/if}
                            <td>{usageCount(row.model?.runs)}</td>
                          </tr>
                        {/each}
                      </tbody>
                    </table>
                  </div>{/if}{:else}<EmptyState
                  density="compact"
                  title={t(
                    'swarm.usageEmpty',
                    'Usage is unavailable until the Swarm has recorded Run activity.',
                  )}
                />{/if}
            </section>
          {:else}<section class="panel" role="tabpanel">
              <h3>{t('swarm.audit', 'Delivery audit')}</h3>
              <ol class="audit">
                {#each events as event (event.id)}<li>
                    <strong>{event.kind}</strong><span
                      >{event.actor} / {date(event.created_at)}</span
                    >
                    {#if event.old || event.new}<dl class="audit-change">
                        {#if event.old}<div>
                            <dt>{t('swarm.audit.previous', 'Previous')}</dt>
                            <dd>{JSON.stringify(event.old)}</dd>
                          </div>{/if}
                        {#if event.new}<div>
                            <dt>{t('swarm.audit.current', 'Current')}</dt>
                            <dd>{JSON.stringify(event.new)}</dd>
                          </div>{/if}
                      </dl>{/if}
                  </li>{:else}<li>
                    <EmptyState
                      density="compact"
                      title={t(
                        'swarm.auditEmpty',
                        'No delivery changes recorded.',
                      )}
                    />
                  </li>{/each}
              </ol>
              {#if eventsCursor}<Button
                  variant="secondary"
                  onClick={loadMoreEvents}
                  >{t('swarm.audit.more', 'Load more events')}</Button
                >{/if}
            </section>{/if}
        {:else}<section class="start">
            <p class="eyebrow">{t('swarm.start', 'Start')}</p>
            <h2>{t('swarm.startTitle', 'Give the group a goal')}</h2>
            <p>
              {t(
                'swarm.startHelp',
                'Every participant starts with this same goal and the selected profile snapshot.',
              )}
            </p>
            <div class="start-profile">
              <FormField
                controlId="swarm-start-profile"
                label={t('swarm.profile', 'Profile')}
              >
                <Dropdown
                  id="swarm-start-profile"
                  ariaLabel={t('swarm.profile', 'Profile')}
                  value={selectedProfile?.id ?? ''}
                  options={profiles.map((profile) => ({
                    value: profile.id,
                    label: profile.name,
                  }))}
                  onValueChange={(id) =>
                    (selectedProfile = profiles.find(
                      (profile) => profile.id === id,
                    ))}
                />
              </FormField>
              <Button
                variant="tertiary"
                icon
                ariaLabel={t('common.edit', 'Edit')}
                tooltip={t('swarm.editProfile', 'Edit profile')}
                disabled={!selectedProfile}
                onClick={() => openProfile(selectedProfile)}
                >{@render actionIcon('edit')}</Button
              >
              <Button
                variant="tertiary"
                icon
                ariaLabel={t('common.delete', 'Delete')}
                tooltip={t('swarm.delete.title', 'Delete profile')}
                disabled={!selectedProfile}
                onClick={() => (deleteCandidate = selectedProfile)}
                >{@render actionIcon('trash')}</Button
              >
            </div>
            <FormField controlId="swarm-goal" label={t('swarm.goal', 'Goal')}>
              <TextArea
                id="swarm-goal"
                value={goal}
                onInput={(value) => (goal = value)}
                rows="6"
                placeholder={t(
                  'swarm.goalPlaceholder',
                  'Describe the work the group should do',
                )}
              />
            </FormField><Button
              variant="primary"
              loading={pending === 'start'}
              disabled={!selectedProfile}
              onClick={startSwarm}
              >{@render actionIcon('play')}{pending === 'start'
                ? t('swarm.starting', 'Starting…')
                : t('swarm.startButton', 'Start Swarm')}</Button
            >
          </section>{/if}
      </section>
    {/if}
  </div>
</main>

{#if composeOpen && selectedSwarm}<Modal
    title={t('swarm.board.openComposer', 'Write post')}
    closeDisabled={posting}
    onClose={() => (composeOpen = false)}
  >
    {#snippet body()}<div class="modal-copy">
        {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
        <FormField
          controlId="swarm-post"
          label={t('swarm.board.post', 'Post to the Board')}
          ><textarea
            class="text-area text-area--default"
            id="swarm-post"
            bind:value={postText}
            rows="3"
            placeholder={t(
              'swarm.board.placeholder',
              'Share a finding or ask the group a question',
            )}></textarea></FormField
        >
        <div class="post-options">
          <FormField
            controlId="swarm-reply"
            label={t('swarm.board.replyTo', 'Reply to post ID (optional)')}
            ><input
              class="s-input"
              id="swarm-reply"
              bind:value={replyTo}
            /></FormField
          ><FormField
            controlId="swarm-pings"
            label={t(
              'swarm.board.pings',
              'Ping participant IDs (comma-separated)',
            )}
            ><input
              class="s-input"
              id="swarm-pings"
              bind:value={postRecipients}
            /></FormField
          >
        </div>
      </div>{/snippet}
    {#snippet footer()}<Button
        variant="secondary"
        disabled={posting}
        onClick={() => (composeOpen = false)}
        >{t('common.cancel', 'Cancel')}</Button
      ><Button
        variant="primary"
        loading={posting}
        disabled={!postText.trim()}
        onClick={post}>{t('swarm.board.submit', 'Post')}</Button
      >{/snippet}
  </Modal>{/if}
{#if swarmDeleteCandidate}<Modal
    title={t('swarm.deleteRun.title', 'Delete Swarm')}
    closeDisabled={pending === 'delete'}
    onClose={() => (swarmDeleteCandidate = null)}
    >{#snippet body()}<div class="modal-copy">
        <p>{swarmDeleteCandidate.prompt}</p>
        <p>
          {t(
            'swarm.deleteRun.body',
            'Permanently delete this Swarm, its Board and participant Sessions? The profile will be kept. This cannot be undone.',
          )}
        </p>
        {#if deleteError}<Banner variant="error">{deleteError}</Banner>{/if}
      </div>{/snippet}{#snippet footer()}<Button
        variant="secondary"
        disabled={pending === 'delete'}
        onClick={() => (swarmDeleteCandidate = null)}
        >{t('common.cancel', 'Cancel')}</Button
      ><Button
        variant="danger"
        loading={pending === 'delete'}
        onClick={deleteSwarm}>{t('common.delete', 'Delete')}</Button
      >{/snippet}</Modal
  >{/if}
{#if deleteCandidate}<Modal
    title={t('swarm.delete.title', 'Delete profile')}
    onClose={() => (deleteCandidate = null)}
    >{#snippet body()}<div class="modal-copy">
        <p>
          {t(
            'swarm.delete.body',
            'Delete {name}? Historical Swarms remain available.',
            { name: deleteCandidate.name },
          )}
        </p>
      </div>{/snippet}{#snippet footer()}<Button
        variant="secondary"
        onClick={() => (deleteCandidate = null)}
        >{t('common.cancel', 'Cancel')}</Button
      ><Button variant="danger" onClick={deleteProfile}
        >{t('common.delete', 'Delete')}</Button
      >{/snippet}</Modal
  >{/if}
{#if settingsOpen}<Modal
    title={t('swarm.communication.title', 'Change communication settings')}
    closeDisabled={pending === 'settings'}
    onClose={() => (settingsOpen = false)}
    >{#snippet body()}<div class="communication">
        {#each ['main', 'discussion', 'ping'] as route (route)}<section>
            <h3>{t(`swarm.delivery.${route}`, route)}</h3>
            <FormField
              controlId={`live-delivery-${route}`}
              label={t('swarm.delivery.mode', 'Mode')}
              ><select
                class="s-input"
                value={deliveryDraft[route].mode}
                onchange={(event) =>
                  changeDelivery(route, 'mode', event.currentTarget.value)}
                ><option value="all"
                  >{t('swarm.delivery.all', 'All messages')}</option
                ><option value="idle"
                  >{t('swarm.delivery.idle', 'When idle')}</option
                ><option value="pull"
                  >{t(
                    'swarm.delivery.pull',
                    'On request or when waking',
                  )}</option
                ></select
              ></FormField
            ><label class="check"
              ><input
                type="checkbox"
                checked={deliveryDraft[route].wake_idle}
                onchange={(event) =>
                  changeDelivery(
                    route,
                    'wake_idle',
                    event.currentTarget.checked,
                  )}
              />
              {t('swarm.delivery.wake', 'Wake idle participants')}</label
            >
          </section>{/each}
        <section>
          <h3>{t('swarm.communication.advanced', 'Advanced')}</h3>
          <div class="advanced-settings">
            <FormField
              controlId="live-coalesce"
              label={t('swarm.delivery.coalesce', 'Coalesce messages (ms)')}
              ><input
                class="s-input"
                id="live-coalesce"
                type="number"
                min="0"
                max="5000"
                value={deliveryDraft.coalesce_ms}
                onchange={(event) =>
                  changeDeliverySetting(
                    'coalesce_ms',
                    event.currentTarget.value,
                  )}
              /></FormField
            >
            <FormField
              controlId="live-batch-messages"
              label={t('swarm.delivery.batchMessages', 'Messages per batch')}
              ><input
                class="s-input"
                id="live-batch-messages"
                type="number"
                min="1"
                max="100"
                value={deliveryDraft.batch_messages}
                onchange={(event) =>
                  changeDeliverySetting(
                    'batch_messages',
                    event.currentTarget.value,
                  )}
              /></FormField
            >
            <FormField
              controlId="live-batch-chars"
              label={t('swarm.delivery.batchChars', 'Characters per batch')}
              ><input
                class="s-input"
                id="live-batch-chars"
                type="number"
                min="16000"
                max="128000"
                value={deliveryDraft.batch_chars}
                onchange={(event) =>
                  changeDeliverySetting(
                    'batch_chars',
                    event.currentTarget.value,
                  )}
              /></FormField
            >
          </div>
        </section>
        <section>
          <h3>{t('swarm.communication.proposed', 'Proposed changes')}</h3>
          {#if settingChanges.length}<ul>
              {#each settingChanges as change (`${change.route}-${change.field}`)}<li
                >
                  {change.route} · {change.field}: <s>{change.before}</s> → {change.after}
                </li>{/each}
            </ul>{:else}<p>
              {t('swarm.communication.noChanges', 'No changes to apply.')}
            </p>{/if}
          <p>
            {t(
              'swarm.communication.effect',
              'Changes affect pending and future delivery only. Already delivered message bodies are not replayed.',
            )}
          </p>
        </section>
      </div>{/snippet}{#snippet footer()}<Button
        variant="secondary"
        disabled={pending === 'settings'}
        onClick={() => (settingsOpen = false)}
        >{t('common.cancel', 'Cancel')}</Button
      ><Button
        variant="primary"
        loading={pending === 'settings'}
        disabled={settingChanges.length === 0}
        onClick={applyDelivery}
        >{pending === 'settings'
          ? t('swarm.applying', 'Applying…')
          : t('swarm.apply', 'Apply changes')}</Button
      >{/snippet}</Modal
  >{/if}
{#if profileSnapshotOpen}<Modal
    title={t('swarm.profileSnapshot', 'Profile snapshot')}
    onClose={() => (profileSnapshotOpen = false)}
    >{#snippet body()}<div class="modal-copy">
        <pre>{JSON.stringify(
            selectedSwarm.profile_snapshot ?? selectedSwarm.profile ?? {},
            null,
            2,
          )}</pre>
      </div>{/snippet}{#snippet footer()}<Button
        variant="primary"
        onClick={() => (profileSnapshotOpen = false)}
        >{t('common.close', 'Close')}</Button
      >{/snippet}</Modal
  >{/if}

<style>
  :global(body) {
    margin: 0;
    background: var(--bg);
    color: var(--text-hi);
    font-family: var(--font-ui);
  }
  .swarm-page {
    display: flex;
    width: 100%;
    height: 100%;
    min-height: 0;
    overflow: hidden;
  }
  .secondary-pane {
    flex-shrink: 0;
  }
  .secondary-pane__header {
    align-items: center;
  }
  .secondary-pane__title {
    color: var(--text-med);
  }
  .secondary-list__item {
    width: 100%;
    text-align: left;
    padding: 10px 12px;
    color: var(--text-hi);
    display: grid;
    gap: 6px;
  }
  .secondary-list__item.active {
    color: var(--accent);
  }
  .secondary-list__item strong {
    font-weight: 500;
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    text-overflow: ellipsis;
  }
  .secondary-list__item span {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }
  .swarms-head {
    margin-top: 16px;
  }
  .sidebar-footer {
    padding: 12px;
    border-top: 1px solid var(--border);
  }
  .sidebar-footer :global(button) {
    width: 100%;
  }
  .workspace {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
    min-height: 0;
  }
  .workspace > :global(.banner) {
    margin: 14px 28px 0;
  }
  .workspace-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;
  }
  .content {
    flex: 1;
    min-width: 0;
    overflow: auto;
    padding: 20px 28px;
  }
  .eyebrow {
    margin: 0;
    color: var(--text-med);
    font: var(--fs-mono-xs) var(--font-mono);
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }
  .swarm-head,
  .section-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
  }
  .swarm-head {
    margin-bottom: 20px;
  }
  .swarm-head h2 {
    font-size: var(--fs-body-md);
    margin: 0 0 8px;
  }
  .goal {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    margin: 0;
  }
  .swarm-head > div:first-child {
    min-width: 0;
    flex: 1;
  }
  .swarm-head {
    align-items: start;
  }
  .swarm-tabs {
    display: flex;
    align-items: center;
    gap: 16px;
  }
  .swarm-tabs :global(.tab-list) {
    flex: 1;
    min-width: 0;
  }
  .context-usage {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--text-med);
    font: var(--fs-mono-xs) var(--font-mono);
    background: transparent;
    border: 0;
  }
  .swarm-identity {
    margin: 0;
  }
  .swarm-identity dt {
    color: var(--text-med);
  }
  .swarm-identity dd {
    margin: 6px 0 0;
    font-family: var(--font-mono);
    overflow-wrap: anywhere;
  }
  .muted,
  .section-head p {
    color: var(--text-med);
    margin: 4px 0;
    overflow-wrap: anywhere;
  }
  .actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .panel {
    display: grid;
    gap: 16px;
    padding-block: 20px;
  }
  .panel h3 {
    margin: 0;
    font-size: var(--fs-heading-sm);
  }
  .start {
    display: grid;
    gap: 20px;
    max-width: 760px;
    margin-inline: auto;
    padding-block: 20px;
  }
  .start h2 {
    font-size: var(--fs-display);
    margin: 0;
  }
  .start > p {
    margin: 0;
    color: var(--text-med);
  }
  .start > :global(button) {
    justify-self: start;
  }
  .start-profile {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    align-items: end;
    gap: 8px;
  }
  .board,
  .audit {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 8px;
  }
  .board li,
  .history {
    padding: 12px;
    border-left: 2px solid var(--border-2);
    background: var(--surface-2);
  }
  .board .post-header {
    display: flex;
    justify-content: space-between;
    gap: 8px;
  }
  .board time,
  .board small {
    color: var(--text-med);
    font: var(--fs-mono-xs) var(--font-mono);
  }
  .post-header {
    align-items: center;
    flex-wrap: wrap;
    border-bottom: 1px solid var(--border-2);
    padding-bottom: 10px;
    margin-bottom: 10px;
  }
  .post-header strong {
    font-size: var(--fs-body-md);
    color: var(--text-hi);
    overflow-wrap: anywhere;
  }
  .post-author {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }
  .participant-avatar {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    min-width: 32px;
    height: 32px;
    padding-inline: 4px;
    box-sizing: border-box;
    border-radius: var(--r-sm);
    color: var(--participant-color);
    background: var(--bg);
    border: 1px solid currentColor;
    font: 600 var(--fs-label-sm) var(--font-ui);
    white-space: nowrap;
  }
  .participant-row strong {
    color: var(--text-hi);
    font-weight: 600;
  }
  .board time {
    white-space: nowrap;
  }
  .board p {
    overflow-wrap: anywhere;
    white-space: pre-wrap;
    margin: 7px 0 0;
  }
  .audit-change {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
    margin: 8px 0 0;
  }
  .audit-change dt {
    color: var(--text-med);
    font: var(--fs-mono-xs) var(--font-mono);
  }
  .audit-change dd {
    margin: 3px 0 0;
    overflow-wrap: anywhere;
    color: var(--text-hi);
    font: var(--fs-mono-xs) var(--font-mono);
  }
  .participant-pane {
    display: grid;
    gap: 10px;
  }
  .participant-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .participant-row :global(button) {
    text-align: left;
    min-width: 0;
    max-width: 100%;
    white-space: normal;
    overflow-wrap: anywhere;
  }
  .participant-pane small,
  .run-indicator {
    display: block;
    color: var(--text-med);
    font: var(--fs-mono-xs) var(--font-mono);
  }
  .run-indicator {
    color: var(--amber);
  }
  .post-options {
    display: grid;
    align-items: end;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }
  .participants {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 8px;
  }
  .participants strong,
  .participants small {
    display: block;
  }
  .participants strong {
    margin-bottom: 5px;
  }
  .participants :global(button[aria-pressed='true']) {
    border-color: var(--accent-40);
    background: var(--accent-06);
  }
  .history {
    margin-top: 4px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
  }
  .history > .section-head {
    flex-wrap: wrap;
    margin-bottom: 16px;
  }
  .participants :global(button) {
    display: flex;
    align-items: center;
    gap: 8px;
    border: 1px solid var(--border-2);
    text-align: left;
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text-lo);
  }
  .dot.running {
    background: var(--amber);
  }
  .history pre,
  .panel pre {
    overflow: auto;
    max-height: 420px;
    white-space: pre-wrap;
    background: var(--bg);
    padding: 10px;
    color: var(--text-hi);
  }
  .audit li {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    padding: 8px;
    border-bottom: 1px solid var(--border-2);
  }
  .modal-copy,
  .communication {
    display: grid;
    gap: 16px;
    padding: 0 18px 18px;
  }
  .communication section {
    display: grid;
    gap: 8px;
  }
  .communication h3 {
    margin: 0;
  }
  .communication p {
    margin: 0;
    color: var(--text-med);
  }
  .communication ul {
    margin: 0;
    padding-left: 18px;
  }
  .advanced-settings {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
  }
  .usage-summary {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
    margin: 0;
  }
  .usage-summary div {
    padding: 12px;
    border-left: 2px solid var(--border-2);
    background: var(--surface-2);
  }
  .usage-summary dt {
    color: var(--text-med);
    font: var(--fs-mono-xs) var(--font-mono);
  }
  .usage-summary dd {
    margin: 5px 0 0;
    color: var(--text-hi);
  }
  .table-wrap {
    overflow-x: auto;
    margin-top: 16px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font: var(--fs-mono-xs) var(--font-mono);
  }
  th,
  td {
    padding: 9px 8px;
    text-align: left;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  th {
    color: var(--text-med);
  }
  td {
    color: var(--text-hi);
  }
  .check {
    display: flex;
    gap: 7px;
    align-items: center;
    color: var(--text-med);
  }
  @media (max-width: 960px) {
    .swarm-page {
      flex-direction: column;
    }
    .secondary-pane {
      width: 100%;
      max-height: 220px;
      border-right: 0;
      border-bottom: 1px solid var(--border);
    }
    .secondary-pane__scroll {
      display: flex;
      align-items: start;
    }
    .secondary-list {
      display: flex;
      gap: 8px;
      flex: 1;
    }
    .secondary-list__item {
      min-width: 160px;
    }
    .swarms-head {
      display: none;
    }
    .content {
      padding: 16px;
    }
  }
  @media (max-width: 640px) {
    .post-options {
      grid-template-columns: 1fr;
    }
    .advanced-settings {
      grid-template-columns: 1fr;
    }
    .usage-summary {
      grid-template-columns: 1fr;
    }
    .content {
      padding: 15px;
    }
    .swarm-head,
    .audit li {
      display: grid;
    }
  }
</style>
