<script>
  import { onMount } from 'svelte';
  import { createExtensionPageClient } from '$lib/extensionPageClient.js';
  import ChatAssistantRun from '../../../../webui/src/components/chat/ChatAssistantRun.svelte';
  import ChatTimelineEntry from '../../../../webui/src/components/chat/ChatTimelineEntry.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';
  import FormField from '../../../../webui/src/components/ui/FormField.svelte';
  import TabList from '../../../../webui/src/components/ui/TabList.svelte';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import EmptyState from '../../../../webui/src/components/ui/EmptyState.svelte';
  import StatusChip from '../../../../webui/src/components/ui/StatusChip.svelte';
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
  let currentSubscription = null;
  const requestId = () =>
    crypto.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
  const page = (value) =>
    Array.isArray(value) ? value : (value?.entries ?? value?.items ?? []);
  const call = (operation, arguments_ = {}) =>
    client.operation(operation, arguments_);
  const isActive = (state) =>
    ['preparing', 'running', 'waiting', 'needs_attention', 'stopping'].includes(
      state,
    );
  const resumableParticipantState = (state) =>
    [
      'prepared',
      'idle',
      'waiting',
      'blocked',
      'failed',
      'interrupted',
    ].includes(state);
  const unfinishedParticipantState = (state) =>
    !['completed', 'done', 'finishing'].includes(state);
  const canResume = $derived(
    selectedSwarm &&
      (['cancelled', 'interrupted'].includes(selectedSwarm.state)
        ? (selectedSwarm.participants ?? []).some((participant) =>
            unfinishedParticipantState(participant.state),
          )
        : selectedSwarm.state !== 'completed' &&
          selectedSwarm.state !== 'stopping' &&
          selectedSwarm.state !== 'preparing' &&
          (selectedSwarm.participants ?? []).some((participant) =>
            resumableParticipantState(participant.state),
          )),
  );
  const tabs = $derived([
    { id: 'board', label: t('swarm.tabs.board', 'Board') },
    { id: 'participants', label: t('swarm.tabs.activity', 'Activity') },
    { id: 'results', label: t('swarm.tabs.results', 'Results') },
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
  const swarmTitle = $derived(
    selectedSwarm
      ? (selectedSwarm.prompt ?? '')
          .split(/\r?\n/)
          .find((line) => line.trim())
          ?.trim()
          .slice(0, 96) || selectedSwarm.id
      : '',
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
  const usageRows = $derived(
    participantUsage.flatMap(({ participant, report }) =>
      (report?.usage?.usage?.models ?? []).map((model) => ({
        participant: participant.display_name,
        model,
      })),
    ),
  );

  function applyContext(next) {
    context = next;
    document.documentElement.lang = init(next.locale);
    for (const [name, value] of Object.entries(next.theme ?? {}))
      document.documentElement.style.setProperty(
        name.startsWith('--') ? name : `--${name}`,
        value,
      );
  }
  async function refresh({ keepSelection = true } = {}) {
    loading = true;
    error = '';
    try {
      const [nextCatalog, nextProfiles, nextSwarms] = await Promise.all([
        call('catalog'),
        call('profiles.list', { limit: 100 }),
        call('swarms.list', { limit: 100 }),
      ]);
      catalog = nextCatalog?.catalog ?? {};
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
      if (selectedSwarm) await selectSwarm(selectedSwarm.id, { silent: true });
    } catch (cause) {
      error =
        cause.message ?? t('swarm.loadError', 'The Swarm page could not load.');
    } finally {
      loading = false;
    }
  }
  async function selectSwarm(id, { silent = false } = {}) {
    if (!silent) error = '';
    try {
      const preservedDiscussion =
        selectedSwarm?.id === id ? selectedDiscussion : '';
      const swarm = (await call('swarms.get', { swarm_id: id })).swarm;
      selectedSwarm = swarm;
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
      await client.replaceRoute(`/swarms/${id}`);
    } catch (cause) {
      if (!silent) error = cause.message;
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
    board = cursor ? [...board, ...page(result)] : page(result);
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
    editor = null;
    await refresh();
    selectedProfile = saved.profile;
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
  async function lifecycle(operation) {
    if (!selectedSwarm) return;
    pending = operation;
    try {
      await call(`swarms.${operation}`, {
        swarm_id: selectedSwarm.id,
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

  async function inspectParticipant(participant) {
    if (!selectedSwarm) return;
    try {
      history = {
        participant,
        data: await client.readHistory(selectedSwarm.id, participant.id, {
          limit: 100,
        }),
      };
      live = [];
      if (participant.lifecycle_run_id) {
        currentSubscription?.();
        const subscription = await client.subscribeRun(
          selectedSwarm.id,
          participant.lifecycle_run_id,
        );
        const key = subscription.subscription_id;
        const off = client.onRunEvent((id, event) => {
          if (id === key) live = [...live, event];
        });
        currentSubscription = () => {
          off();
          if (key) client.unsubscribeRun(key);
        };
      }
    } catch (cause) {
      error = cause.message;
    }
  }
  function routeSelection(route) {
    const match = /^\/swarms\/([^/]+)$/.exec(route ?? '');
    if (match) selectSwarm(match[1]);
  }
  onMount(() => {
    client ??= bridgeClient ?? createExtensionPageClient();
    const offContext = client.onContext((next) => {
      applyContext(next);
      routeSelection(next.route);
      refresh();
    });
    const offInvalidation = client.onInvalidation(() => refresh());
    return () => {
      currentSubscription?.();
      offContext();
      offInvalidation();
      client.dispose();
    };
  });
</script>

<svelte:head><title>{t('swarm.title', 'Swarms')}</title></svelte:head>
<main class="view-frame swarm-page">
  <header class="view-header">
    <div>
      <p class="eyebrow">{t('swarm.extension', 'Extension')}</p>
      <h1>{t('swarm.title', 'Swarms')}</h1>
      <p class="lede">
        {t(
          'swarm.lede',
          'Independent Sessions coordinating around one shared goal.',
        )}
      </p>
    </div>
    <Button variant="tertiary" disabled={loading} onClick={() => refresh()}
      >{t('common.refresh', 'Refresh')}</Button
    >
  </header>
  {#if error}<Banner variant="error" role="alert"
      >{error}<Button variant="tertiary" onClick={() => (error = '')}
        >{t('common.dismiss', 'Dismiss')}</Button
      ></Banner
    >{/if}
  {#if loading}<Banner variant="info" role="status"
      >{t('swarm.loading', 'Loading Swarms…')}</Banner
    >{:else if editor}<ProfileEditor
      profile={editor === 'new' ? null : editor}
      {catalog}
      onSave={saveProfile}
      onCancel={() => (editor = null)}
    />{:else}
    <div class="layout">
      <aside>
        <div class="list-head">
          <div>
            <p class="eyebrow">{t('swarm.profiles', 'Profiles')}</p>
            <strong
              >{t('swarm.profileCount', '{count} saved', {
                count: profiles.length,
              })}</strong
            >
          </div>
          <Button variant="primary" onClick={() => (editor = 'new')}
            >{t('swarm.newProfile', 'New profile')}</Button
          >
        </div>
        {#if profiles.length === 0}<EmptyState
            density="compact"
            title={t('swarm.noProfiles', 'No profiles yet.')}
            description={t(
              'swarm.noProfilesHelp',
              'Create a profile to start a Swarm.',
            )}
          />{:else}<div class="profile-list">
            {#each profiles as profile (profile.id)}<Button
                variant="tertiary"
                class={selectedProfile?.id === profile.id ? 'chosen' : ''}
                onClick={() => (selectedProfile = profile)}
                ><strong>{profile.name}</strong><span
                  >{profile.slug} / / r{profile.revision}</span
                ></Button
              >{/each}
          </div>{/if}{#if profilesCursor}<Button
            variant="tertiary"
            onClick={loadMoreProfiles}
            >{t('swarm.profiles.more', 'Load more profiles')}</Button
          >{/if}{#if selectedProfile}<div class="profile-actions">
            <Button
              variant="tertiary"
              onClick={() => (editor = selectedProfile)}
              >{t('common.edit', 'Edit')}</Button
            ><Button
              variant="danger"
              onClick={() => (deleteCandidate = selectedProfile)}
              >{t('common.delete', 'Delete')}</Button
            >
          </div>{/if}
        <div class="list-head swarms-head">
          <div>
            <p class="eyebrow">{t('swarm.retained', 'Retained Swarms')}</p>
            <strong
              >{t('swarm.total', '{count} total', {
                count: swarms.length,
              })}</strong
            >
          </div>
        </div>
        <div class="profile-list">
          {#each swarms as swarm (swarm.id)}<Button
              variant="tertiary"
              class={selectedSwarm?.id === swarm.id ? 'chosen' : ''}
              onClick={() => selectSwarm(swarm.id)}
              ><strong>{swarm.title ?? swarm.id}</strong><span
                >{swarm.state} / / {swarm.done_count}/{swarm.participant_count}
                {t('swarm.done', 'done')}</span
              ></Button
            >{/each}
        </div>
        {#if swarmsCursor}<Button variant="tertiary" onClick={loadMoreSwarms}
            >{t('swarm.swarms.more', 'Load more Swarms')}</Button
          >{/if}
      </aside>
      <section class="content">
        {#if selectedSwarm}<div class="swarm-head">
            <div>
              <StatusChip
                variant={isActive(selectedSwarm.state) ? 'warn' : 'neutral'}
                >{selectedSwarm.state}</StatusChip
              >
              <h2>{swarmTitle}</h2>
              <p class="goal">{selectedSwarm.id}</p>
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
                  >{pending === 'stop'
                    ? t('swarm.stopping', 'Stopping...')
                    : t('swarm.stop', 'Stop')}</Button
                >{/if}{#if canResume}<Button
                  variant="primary"
                  loading={pending === 'resume'}
                  disabled={pending === 'stop'}
                  onClick={() => lifecycle('resume')}
                  >{pending === 'resume'
                    ? t('swarm.resuming', 'Resuming...')
                    : t('swarm.resume', 'Resume')}</Button
                >{/if}<Button
                variant="tertiary"
                onClick={() => (profileSnapshotOpen = true)}
                >{t(
                  'swarm.profileSnapshot',
                  'Inspect profile snapshot',
                )}</Button
              ><Button variant="tertiary" onClick={openDelivery}
                >{t(
                  'swarm.changeCommunication',
                  'Change communication settings',
                )}</Button
              >
            </div>
          </div>
          <TabList
            items={tabs}
            value={activeTab}
            ariaLabel={t('swarm.details', 'Swarm details')}
            onChange={(next) => (activeTab = next)}
          />
          {#if activeTab === 'board'}<section class="panel" role="tabpanel">
              <div class="section-head">
                <h3>{t('swarm.board.title', 'Board')}</h3>
                <FormField
                  controlId="swarm-discussion"
                  label={t('swarm.board.discussion', 'Discussion')}
                  ><select
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
                    variant="tertiary"
                    onClick={() =>
                      loadDiscussions(selectedSwarm, discussionCursor)}
                    >{t(
                      'swarm.board.moreDiscussions',
                      'Load more discussions',
                    )}</Button
                  >{/if}
              </div>
              {#if board.length === 0}<EmptyState
                  density="compact"
                  title={t('swarm.board.empty', 'No Board messages yet.')}
                />{:else}<ol class="board">
                  {#each board as post (post.id)}<li>
                      <div>
                        <strong
                          >{post.author_name ??
                            post.author?.name ??
                            t('swarm.participant', 'Participant')}</strong
                        ><span>{date(post.created_at)}</span>
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
                    variant="tertiary"
                    onClick={() =>
                      loadBoard(selectedSwarm, selectedDiscussion, boardCursor)}
                    >{t('swarm.board.more', 'Load earlier messages')}</Button
                  >{/if}{/if}
              <div class="board-bottom">
                <div class="post-composer">
                  <Button
                    variant="tertiary"
                    onClick={() => (composeOpen = !composeOpen)}
                    >{composeOpen
                      ? t('swarm.board.closeComposer', 'Close composer')
                      : t('swarm.board.openComposer', 'Write post')}</Button
                  >
                  {#if composeOpen}<FormField
                      controlId="swarm-post"
                      label={t('swarm.board.post', 'Post to the Board')}
                      ><textarea
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
                        label={t(
                          'swarm.board.replyTo',
                          'Reply to post ID (optional)',
                        )}
                        ><input
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
                          id="swarm-pings"
                          bind:value={postRecipients}
                        /></FormField
                      >
                    </div>
                    <Button
                      variant="primary"
                      loading={posting}
                      disabled={!postText.trim()}
                      onClick={post}
                      >{posting
                        ? t('swarm.board.posting', 'Posting...')
                        : t('swarm.board.submit', 'Post')}</Button
                    >{/if}
                </div>
                <section
                  class="participant-pane"
                  aria-label={t('swarm.participants', 'Participants')}
                >
                  <p class="eyebrow">
                    {t('swarm.participants', 'Participants')}
                  </p>
                  {#each selectedSwarm.participants ?? [] as participant (participant.id)}<Button
                      variant="tertiary"
                      onClick={() => inspectParticipant(participant)}
                      ><span
                        ><strong>{participant.display_name}</strong><small
                          >{participant.model} / {participant.state} / {participant.pending_count ??
                            0}
                          {t('swarm.pending', 'pending')}</small
                        ></span
                      >{#if participant.run_active}<span class="run-indicator"
                          >{t('swarm.runActive', 'Run active')}</span
                        >{/if}</Button
                    >{/each}
                </section>
              </div>
            </section>
          {:else if activeTab === 'participants'}<section
              class="panel"
              role="tabpanel"
            >
              <h3>{t('swarm.participants', 'Participants')}</h3>
              <div class="participants">
                {#each selectedSwarm.participants ?? [] as participant (participant.id)}<Button
                    variant="tertiary"
                    onClick={() => inspectParticipant(participant)}
                    ><span
                      class:running={participant.state === 'running'}
                      class="dot"
                    ></span><span
                      ><strong>{participant.display_name}</strong><small
                        >{participant.model} / / {participant.state} / / {participant.pending_count ??
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
                    <Button variant="tertiary" onClick={() => (history = null)}
                      >{t('common.close', 'Close')}</Button
                    >
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
          {:else if activeTab === 'results'}<section
              class="panel"
              role="tabpanel"
            >
              <h3>{t('swarm.results', 'Results')}</h3>
              {#each selectedSwarm.participants?.filter((item) => item.summary) ?? [] as participant (participant.id)}<article
                  class="result"
                >
                  <strong>{participant.display_name}</strong>
                  <p>{participant.summary}</p>
                  {#if participant.artifacts?.length}<ul>
                      {#each participant.artifacts as artifact (artifact)}<li>
                          {artifact}
                        </li>{/each}
                    </ul>{/if}
                </article>{:else}<EmptyState
                  density="compact"
                  title={t(
                    'swarm.resultsEmpty',
                    'Completed participant summaries will appear here.',
                  )}
                />{/each}
            </section>
          {:else if activeTab === 'usage'}<section
              class="panel"
              role="tabpanel"
            >
              <h3>{t('swarm.usage', 'Usage')}</h3>
              {#if usage?.usage}<dl class="usage-summary">
                  <div>
                    <dt>
                      {t('swarm.usage.measuredTokens', 'Measured tokens')}
                    </dt>
                    <dd>
                      {usage.usage.usage?.totals
                        ? usage.usage.usage.totals.measured_input_tokens +
                          usage.usage.usage.totals.measured_output_tokens
                        : t('swarm.usage.unavailable', 'Unavailable')}
                    </dd>
                  </div>
                  <div>
                    <dt>
                      {t('swarm.usage.estimatedTokens', 'Estimated tokens')}
                    </dt>
                    <dd>
                      {usage.usage.usage?.totals
                        ? usage.usage.usage.totals.estimated_input_tokens +
                          usage.usage.usage.totals.estimated_output_tokens
                        : t('swarm.usage.unavailable', 'Unavailable')}
                    </dd>
                  </div>
                  <div>
                    <dt>{t('swarm.usage.toolCalls', 'Tool Calls')}</dt>
                    <dd>
                      {usage.usage.tools?.total_calls ??
                        t('swarm.usage.unavailable', 'Unavailable')}
                    </dd>
                  </div>
                </dl>
                {#if usageRows.length}<div class="table-wrap">
                    <table>
                      <thead
                        ><tr
                          ><th>{t('swarm.usage.participant', 'Participant')}</th
                          ><th>{t('swarm.usage.model', 'Model')}</th><th
                            >{t(
                              'swarm.usage.measuredTokens',
                              'Measured tokens',
                            )}</th
                          ><th
                            >{t(
                              'swarm.usage.estimatedTokens',
                              'Estimated tokens',
                            )}</th
                          ><th>{t('swarm.usage.runs', 'Runs')}</th></tr
                        ></thead
                      >
                      <tbody
                        >{#each usageRows as row (`${row.participant}:${row.model.provider}:${row.model.model}`)}<tr
                            ><td>{row.participant}</td><td
                              >{row.model.provider}/{row.model.model}</td
                            ><td
                              >{row.model.measured_input_tokens +
                                row.model.measured_output_tokens}</td
                            ><td
                              >{row.model.estimated_input_tokens +
                                row.model.estimated_output_tokens}</td
                            ><td>{row.model.runs}</td></tr
                          >{/each}</tbody
                      >
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
                  variant="tertiary"
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
            <FormField controlId="swarm-goal" label={t('swarm.goal', 'Goal')}
              ><textarea
                id="swarm-goal"
                bind:value={goal}
                rows="6"
                placeholder={t(
                  'swarm.goalPlaceholder',
                  'Describe the work the group should do',
                )}></textarea></FormField
            ><Button
              variant="primary"
              loading={pending === 'start'}
              disabled={!selectedProfile}
              onClick={startSwarm}
              >{pending === 'start'
                ? t('swarm.starting', 'Starting…')
                : t('swarm.startButton', 'Start Swarm')}</Button
            >
          </section>{/if}
      </section>
    </div>{/if}
</main>

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
        variant="tertiary"
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
                value={deliveryDraft[route].mode}
                onchange={(event) =>
                  changeDelivery(route, 'mode', event.currentTarget.value)}
                ><option value="all"
                  >{t('swarm.delivery.all', 'All messages')}</option
                ><option value="idle"
                  >{t('swarm.delivery.idle', 'When idle')}</option
                ><option value="pull"
                  >{t('swarm.delivery.pull', 'Pull only')}</option
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
                  {change.route} / / {change.field}: <s>{change.before}</s> → {change.after}
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
        variant="tertiary"
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
    max-width: 1500px;
    margin: auto;
    padding: 24px;
    min-height: 100vh;
  }
  .view-header,
  .swarm-head,
  .list-head,
  .section-head {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    align-items: flex-start;
  }
  .eyebrow {
    margin: 0;
    color: var(--accent);
    font: var(--fs-mono-xs) var(--font-mono);
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .lede,
  .goal,
  .muted,
  .section-head p {
    color: var(--text-med);
    margin-top: 5px;
  }
  .layout {
    display: grid;
    grid-template-columns: 290px minmax(0, 1fr);
    gap: 22px;
    margin-top: 24px;
  }
  aside,
  .content,
  .panel,
  .start {
    border: 1px solid var(--border-2);
    background: var(--surface);
    border-radius: 8px;
  }
  .content {
    min-height: 600px;
    padding: 22px;
  }
  .list-head {
    padding: 15px;
    border-bottom: 1px solid var(--border-2);
  }
  .swarms-head {
    margin-top: 13px;
  }
  .profile-list {
    display: grid;
    padding: 8px;
  }
  .profile-list :global(button) {
    display: grid;
    text-align: left;
    justify-items: start;
  }
  .profile-list :global(.chosen) {
    background: var(--surface-3);
  }
  .profile-list span,
  .participants small,
  .audit span {
    font: var(--fs-mono-xs) var(--font-mono);
    color: var(--text-med);
  }
  .profile-actions,
  .actions {
    display: flex;
    gap: 8px;
    padding: 8px 15px;
  }
  .actions {
    padding: 0;
    flex-wrap: wrap;
  }
  .swarm-head h2 {
    margin: 8px 0 0;
  }
  .panel,
  .start {
    padding: 16px;
    display: grid;
    gap: 14px;
    margin-top: 18px;
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
  .result,
  .history {
    padding: 12px;
    border-left: 2px solid var(--border-2);
    background: var(--surface-2);
  }
  .board li > div {
    display: flex;
    justify-content: space-between;
    gap: 8px;
  }
  .board li span,
  .board small {
    color: var(--text-med);
    font: var(--fs-mono-xs) var(--font-mono);
  }
  .board p {
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
  .board-bottom {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(230px, 0.38fr);
    gap: 14px;
  }
  .participant-pane {
    border-left: 2px solid var(--border-2);
    padding-left: 12px;
    display: grid;
    align-content: start;
    gap: 4px;
  }
  .participant-pane :global(button) {
    justify-content: space-between;
    text-align: left;
  }
  .participant-pane small,
  .run-indicator {
    display: block;
    color: var(--text-med);
    font: var(--fs-mono-xs) var(--font-mono);
  }
  .run-indicator {
    color: var(--warning);
  }
  .post-composer {
    display: grid;
    gap: 8px;
  }
  .post-options {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }
  .participants {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 8px;
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
  .start {
    max-width: 760px;
    padding: 28px;
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
  @media (max-width: 760px) {
    .swarm-page {
      padding: 12px;
    }
    .layout,
    .post-options {
      grid-template-columns: 1fr;
    }
    .advanced-settings {
      grid-template-columns: 1fr;
    }
    .usage-summary {
      grid-template-columns: 1fr;
    }
    .board-bottom {
      grid-template-columns: 1fr;
    }
    .participant-pane {
      border-left: 0;
      border-top: 2px solid var(--border-2);
      padding: 12px 0 0;
    }
    .content {
      padding: 15px;
    }
    .swarm-head,
    .board li > div,
    .audit li {
      display: grid;
    }
  }
</style>
