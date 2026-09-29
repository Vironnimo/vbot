<script>
  import {
    participantColor,
    participantInitials,
    participantState,
    participantDetails,
    participantTotal,
    profileTooltip,
    runTitle,
    runTooltip,
    resumableParticipantState,
    tokensUsed,
    usageCount,
  } from './pagePresentation.js';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import EmptyState from '../../../../webui/src/components/ui/EmptyState.svelte';
  import { tooltip } from '../../../../webui/src/lib/tooltip.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import ProfileEditor from './ProfileEditor.svelte';
  import TabList from '../../../../webui/src/components/ui/TabList.svelte';
  import StatusChip from '../../../../webui/src/components/ui/StatusChip.svelte';
  import FormField from '../../../../webui/src/components/ui/FormField.svelte';
  import MarkdownContent from '../../../../webui/src/components/chat/MarkdownContent.svelte';
  import ChatAssistantRun from '../../../../webui/src/components/chat/ChatAssistantRun.svelte';
  import ChatTimelineEntry from '../../../../webui/src/components/chat/ChatTimelineEntry.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';
  import { provideNavigation } from '../../../../webui/src/lib/navigation.svelte.js';
  import { createSwarmPageModel } from './pageModel.svelte.js';
  import { createSwarmPageActivity } from './pageActivity.svelte.js';
  import './swarmPage.css';
  import WikiPanel from './WikiPanel.svelte';
  let wikiPanel = $state(null);

  let { bridgeClient = null } = $props();
  const model = createSwarmPageModel({
    get bridgeClient() {
      return bridgeClient;
    },
    get wiki() {
      return wikiPanel;
    },
    get activity() {
      return activity;
    },
  });
  const activity = createSwarmPageActivity({
    get model() {
      return model;
    },
  });
  // The page's dialogs register as layers of the app's navigation, so Back
  // and Forward close them before leaving the page's place.
  provideNavigation({ registerLayer: model.client.registerLayer });
  // The goal form is the page's home: "New run" is its navigation entry.
  const startVisible = $derived(!model.editor && !model.selectedSwarm);
  // Same route names as the profile editor's delivery settings.
  const routeLabels = $derived({
    main: t('swarm.profile.mainMessages'),
    discussion: t('swarm.profile.joinedMessages'),
    ping: t('swarm.profile.mentions'),
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

{#snippet participantChip(participant, selected = undefined)}
  <Button
    variant="tertiary"
    class="participant-chip"
    style={`--participant-color: ${participantColor(participant.id)}`}
    tooltip={participantDetails(participant)}
    ariaLabel={participantDetails(participant)}
    aria-pressed={selected}
    onClick={() => activity.inspectParticipant(participant)}
  >
    {@render participantAvatar(participant.id, participant.display_name)}
    <strong>{participant.display_name}</strong>
    <span
      class="participant-status"
      data-state={participantState(participant)}
      aria-hidden="true"
    ></span>
  </Button>
{/snippet}

<svelte:head><title>{t('swarm.title')}</title></svelte:head>
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
    {:else if kind === 'folder'}<path d="M3 7V5h6l2 2h10v13H3Z" />
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
      <span class="secondary-pane__title">{t('swarm.profiles')}</span>
      <Button
        variant="tertiary"
        icon
        ariaLabel={t('swarm.newProfile')}
        tooltip={t('swarm.newProfile')}
        loading={model.pending === 'profile'}
        onClick={() => model.navigate(() => model.openProfile())}
        >{@render actionIcon('plus')}</Button
      >
    </div>
    <div class="swarm-sidebar-start">
      <button
        type="button"
        class="secondary-list__item swarm-new-run"
        class:active={startVisible}
        aria-current={startVisible ? 'page' : undefined}
        onclick={() => model.newSwarm()}
        >{@render actionIcon('play')}<span>{t('swarm.newRun')}</span></button
      >
    </div>
    <div class="secondary-pane__scroll">
      <nav class="secondary-list" aria-label={t('swarm.profiles')}>
        {#each model.profiles as profile (profile.id)}
          <button
            class="secondary-list__item"
            class:active={model.editor?.id === profile.id}
            aria-current={model.editor?.id === profile.id ? 'page' : undefined}
            use:tooltip={profileTooltip(profile)}
            onclick={() => model.navigate(() => model.openProfile(profile))}
          >
            <span class="sidebar-title"
              >{profile.name} ({participantTotal(profile)})</span
            >
          </button>
        {:else}<EmptyState
            density="compact"
            title={t('swarm.noProfiles')}
            description={t('swarm.noProfilesHelp')}
          />{/each}
        {#if model.profilesCursor}<Button
            variant="tertiary"
            onClick={model.loadMoreProfiles}>{t('swarm.profiles.more')}</Button
          >{/if}
      </nav>
      {#each model.runGroups as group (group.id)}
        <section class="run-group" aria-labelledby={`swarm-runs-${group.id}`}>
          <h3 class="run-group__title" id={`swarm-runs-${group.id}`}>
            {group.label}
          </h3>
          <nav class="secondary-list" aria-label={group.label}>
            {#each group.entries as swarm (swarm.id)}
              <button
                class="secondary-list__item"
                class:active={!model.editor &&
                  model.selectedSwarm?.id === swarm.id}
                use:tooltip={runTooltip(swarm)}
                onclick={() => model.navigate(() => model.openSwarm(swarm.id))}
              >
                <span class="sidebar-title">{runTitle(swarm)}</span>
              </button>
            {:else}
              <p class="run-group__empty">{group.empty}</p>
            {/each}
          </nav>
        </section>
      {/each}
      {#if model.swarmsCursor}<Button
          variant="tertiary"
          onClick={model.loadMoreSwarms}>{t('swarm.swarms.more')}</Button
        >{/if}
    </div>
  </aside>
  <div class="workspace">
    {#if model.error}<Banner variant="error" role="alert">{model.error}</Banner
      >{/if}
    {#if model.loading}<Banner variant="info" role="status"
        >{t('swarm.loading')}</Banner
      >{/if}
    {#if model.editor}
      {#key model.editorKey}
        <ProfileEditor
          bind:this={model.profileEditor}
          profile={model.editor === 'new' ? null : model.editor}
          catalog={model.catalog}
          bridgeClient={model.client}
          onSave={model.saveProfile}
          onCancel={model.newSwarm}
          onDelete={model.confirmProfileDelete}
        />
      {/key}
    {:else}
      <section class="content">
        {#if model.selectedSwarm}<header class="swarm-head view-header">
            <div class="swarm-heading view-header__intro">
              <div class="swarm-heading-title">
                <h2 class="view-header__title">
                  {model.selectedSwarm.profile_snapshot?.name ||
                    t('swarm.profile')}
                </h2>
                <StatusChip variant={model.working ? 'warn' : 'neutral'}>
                  {model.selectedSwarm.state}
                </StatusChip>
              </div>
            </div>
            <div class="view-header__actions actions">
              {#if model.working}<Button
                  variant="danger"
                  loading={model.pending === 'stop'}
                  onClick={() => model.lifecycle('stop')}
                  >{@render actionIcon('stop')}{model.pending === 'stop'
                    ? t('swarm.stopping')
                    : t('swarm.stop')}</Button
                >{:else if model.canResume}<Button
                  variant="primary"
                  loading={model.pending === 'resume'}
                  disabled={!!model.pending}
                  onClick={() => model.lifecycle('resume')}
                  >{@render actionIcon('play')}{model.pending === 'resume'
                    ? t('swarm.resuming')
                    : t('swarm.resume')}</Button
                >{/if}<Button
                variant="danger"
                disabled={!!model.pending || model.working}
                tooltip={model.working
                  ? t('swarm.deleteRun.stopFirst')
                  : t('swarm.deleteRun.title')}
                onClick={() => {
                  model.deleteError = '';
                  model.swarmDeleteCandidate = model.selectedSwarm;
                }}
                ariaLabel={t('swarm.deleteRun.title')}
                icon>{@render actionIcon('trash')}</Button
              ><Button
                variant="secondary"
                onClick={() => (model.profileSnapshotOpen = true)}
                icon
                ariaLabel={t('swarm.inspectProfileSnapshot')}
                tooltip={t('swarm.inspectProfileSnapshot')}
                >{@render actionIcon('document')}</Button
              ><Button
                variant="tertiary"
                icon
                onClick={model.openDelivery}
                ariaLabel={t('swarm.changeCommunication')}
                tooltip={t('swarm.changeCommunication')}
                >{@render actionIcon('settings')}</Button
              >
            </div>
          </header>
          <div class="swarm-tabs">
            <TabList
              items={model.tabs}
              value={model.activeTab}
              ariaLabel={t('swarm.details')}
              onChange={(next) => model.navigate(() => model.openTab(next))}
            />
          </div>
          {#if model.selectedSwarm.participants?.some( (participant) => ['failed', 'interrupted'].includes(participant.state) )}
            <Banner variant="error" role="alert">
              {t('swarm.participants.failed', {
                names: model.selectedSwarm.participants
                  .filter((participant) =>
                    ['failed', 'interrupted'].includes(participant.state),
                  )
                  .map((participant) => participant.display_name)
                  .join(', '),
              })}
            </Banner>
          {/if}
          {#key model.selectedSwarm.id}<WikiPanel
              bind:this={wikiPanel}
              swarmId={model.selectedSwarm.id}
              client={model.client}
              contentLinks={model.contentLinks}
              active={model.activeTab === 'wiki'}
            />{/key}
          {#if model.activeTab === 'board'}<section
              class="panel board-panel"
              role="tabpanel"
            >
              <div class="section-head board-toolbar">
                <FormField
                  controlId="swarm-discussion"
                  label={t('swarm.board.discussion')}
                  ><select
                    class="s-input"
                    id="swarm-discussion"
                    value={model.selectedDiscussion}
                    onchange={(event) =>
                      model.chooseDiscussion(event.currentTarget.value)}
                    >{#each model.discussionOptions as discussion (discussion.id)}<option
                        value={discussion.id}
                        >{discussion.title ??
                          discussion.name ??
                          discussion.id}</option
                      >{/each}</select
                  ></FormField
                >
                {#if model.discussionCursor}<Button
                    variant="secondary"
                    onClick={() =>
                      model.loadDiscussions(
                        model.selectedSwarm,
                        model.discussionCursor,
                      )}>{t('swarm.board.moreDiscussions')}</Button
                  >{/if}
                <Button
                  variant="secondary"
                  onClick={() => (model.composeOpen = true)}
                >
                  {@render actionIcon('edit')}{t('swarm.board.openComposer')}
                </Button>
              </div>
              <div class="board-context">
                <details
                  class="swarm-goal-post"
                  use:model.contentLinks={{ references: false }}
                >
                  <summary>{t('swarm.board.goal')}</summary>
                  <MarkdownContent
                    source={model.selectedSwarm.prompt}
                    class="msg-markdown"
                  />
                </details>
                {#if model.selectedSwarm.effective_configuration?.cwd}
                  <div
                    class="board-directory"
                    use:tooltip={{
                      text: model.selectedSwarm.effective_configuration.cwd,
                      mono: true,
                      selectable: true,
                    }}
                  >
                    {@render actionIcon('folder')}
                    <span
                      >{model.selectedSwarm.effective_configuration.cwd}</span
                    >
                  </div>
                {/if}
              </div>
              <section
                class="participant-pane"
                aria-label={t('swarm.participants')}
              >
                <p class="section-label">
                  {t('swarm.participants')}
                  <span class="participant-count"
                    >{model.discussionParticipants.length}</span
                  >
                </p>
                <div class="participant-row">
                  {#each model.discussionParticipants as participant (participant.id)}
                    {@render participantChip(participant)}
                  {:else}
                    <span class="muted">{t('swarm.board.noParticipants')}</span>
                  {/each}
                </div>
              </section>
              {#if model.board.length === 0}<EmptyState
                  density="compact"
                  title={t('swarm.board.empty')}
                />{:else}<ol class="board" use:model.contentLinks>
                  {#each model.board as post (post.id)}<li
                      data-post-number={post.sequence}
                      style:--participant-color={post.author?.kind ===
                        'participant' && post.author?.id
                        ? participantColor(post.author.id)
                        : 'var(--text-med)'}
                    >
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
                              t('swarm.participant')}</strong
                          >
                        </div>
                        <span class="post-meta"
                          >{#if post.sequence != null}<span class="post-number"
                              >#{post.sequence}</span
                            >{/if}<time datetime={post.created_at}
                            >{model.date(post.created_at)}</time
                          ></span
                        >
                      </div>
                      {#if post.discussion_announcement}
                        <div class="discussion-announcement">
                          <p>
                            {t('swarm.board.discussionOpened', {
                              name: post.author.name,
                            })}
                          </p>
                          <Button
                            variant="secondary"
                            onClick={() =>
                              model.openDiscussion(
                                post.discussion_announcement,
                              )}
                          >
                            {post.discussion_announcement.title}
                          </Button>
                        </div>
                      {:else}<MarkdownContent
                          source={post.text}
                          class="msg-markdown"
                        />{/if}
                      {#if post.reply_sequence != null}<small
                          ><a href="#post/{post.reply_sequence}"
                            >{t('swarm.board.reply', {
                              id: `#${post.reply_sequence}`,
                            })}</a
                          ></small
                        >{:else if post.reply_to}<small
                          >{t('swarm.board.reply', {
                            id: post.reply_to,
                          })}</small
                        >{/if}{#if post.recipients?.length}<small
                          >{t('swarm.board.addressed', {
                            names: post.recipients
                              .map(
                                (id) =>
                                  model.selectedSwarm?.participants?.find(
                                    (participant) => participant.id === id,
                                  )?.display_name ?? id,
                              )
                              .join(', '),
                          })}</small
                        >{/if}
                    </li>{/each}
                </ol>
                {#if model.boardCursor}<Button
                    variant="secondary"
                    onClick={() =>
                      model.loadBoard(
                        model.selectedSwarm,
                        model.selectedDiscussion,
                        model.boardCursor,
                      )}>{t('swarm.board.more')}</Button
                  >{/if}{/if}
            </section>
          {:else if model.activeTab === 'participants'}<section
              class="panel"
              role="tabpanel"
            >
              <h3>{t('swarm.participants')}</h3>
              <div class="participants participant-row">
                {#each model.selectedSwarm.participants ?? [] as participant (participant.id)}
                  {@render participantChip(
                    participant,
                    activity.history?.participant.id === participant.id,
                  )}
                {/each}
              </div>
              {#if activity.history}<article
                  class="history"
                  use:model.contentLinks
                >
                  <div class="section-head">
                    <h3>
                      {activity.history.participant.display_name}
                      {t('swarm.activity')}
                    </h3>
                    <div class="actions">
                      <button
                        class="context-usage"
                        aria-label={t('chat.contextRingLabel')}
                        use:tooltip={activity.activityContextTooltip}
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
                            stroke-dasharray={`${activity.contextRatio} 1`}
                            transform="rotate(-90 9 9)"
                          /></svg
                        >
                        {t('swarm.context')}: {activity.contextTokens}
                      </button>
                      {#if model.canResume && activity.selectedParticipant && !activity.selectedParticipant.run_active && resumableParticipantState(activity.selectedParticipant.state)}
                        <Button
                          variant="primary"
                          disabled={Boolean(model.pending)}
                          onClick={() =>
                            model.lifecycle(
                              'resume',
                              activity.selectedParticipant.id,
                            )}>{t('swarm.resumeParticipant')}</Button
                        >
                      {/if}
                      <Button
                        variant="tertiary"
                        icon
                        ariaLabel={t('common.close')}
                        tooltip={t('common.close')}
                        onClick={activity.leaveActivity}
                        >{@render actionIcon('close')}</Button
                      >
                    </div>
                  </div>
                  {#if activity.history.data.has_more && activity.history.data.next_before}
                    <Button
                      variant="secondary"
                      disabled={activity.historyLoading}
                      onClick={activity.loadEarlierActivity}
                    >
                      {t('swarm.loadOlderMessages')}
                    </Button>
                  {/if}
                  {#each activity.activityTimeline as item (item.id)}
                    {#if item.type === 'assistant_run'}<ChatAssistantRun
                        {item}
                        isReasoningOpen={activity.isReasoningOpen}
                        onReasoningOpenChange={activity.setReasoningOpen}
                        onCancelToolCall={activity.cancelToolCall}
                        agentName={activity.history.participant.display_name}
                      />{:else}<ChatTimelineEntry
                        {item}
                        agentName={activity.history.participant.display_name}
                        messageEditingDisabled
                      />{/if}
                  {:else}<EmptyState
                      density="compact"
                      title={t('swarm.noActivity')}
                    />{/each}
                </article>{/if}
            </section>
          {:else if model.activeTab === 'usage'}<section
              class="panel"
              role="tabpanel"
            >
              <h3>{t('swarm.usage')}</h3>
              <dl class="swarm-identity">
                <dt>{t('swarm.id')}</dt>
                <dd>{model.selectedSwarm.id}</dd>
              </dl>
              {#if model.usage?.usage}<dl class="usage-summary">
                  <div>
                    <dt>
                      {t('swarm.usage.tokensUsed')}
                    </dt>
                    <dd>{tokensUsed(model.usage.usage.usage?.totals)}</dd>
                  </div>
                  <div>
                    <dt>{t('swarm.usage.toolCalls')}</dt>
                    <dd>
                      {usageCount(model.usage.usage.tools?.total_calls)}
                    </dd>
                  </div>
                </dl>
                {#if model.usageRows.length}<div class="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>{t('swarm.usage.participant')}</th>
                          <th>{t('swarm.usage.model')}</th>
                          <th>{t('swarm.usage.tokensUsed')}</th>
                          <th>{t('swarm.usage.toolCalls')}</th>
                          <th>{t('swarm.usage.runs')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {#each model.usageRows as row (row.id)}
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
                  </div>{/if}{:else if !model.usageLoading}<EmptyState
                  density="compact"
                  title={t('swarm.usageEmpty')}
                />{/if}
            </section>
          {/if}
        {:else}<section class="start">
            <header class="view-header">
              <div class="view-header__intro">
                <h2 class="view-header__title">
                  {t('swarm.startTitle')}
                </h2>
                <p class="view-header__subtitle">
                  {t('swarm.startHelp')}
                </p>
              </div>
              <div class="view-header__actions">
                <Button
                  variant="tertiary"
                  icon
                  ariaLabel={t('common.refresh')}
                  tooltip={t('common.refresh')}
                  disabled={model.loading}
                  onClick={() => model.refresh()}
                  >{@render actionIcon('refresh')}</Button
                >
              </div>
            </header>
            <div class="start-profile">
              <FormField
                controlId="swarm-start-profile"
                label={t('swarm.profile')}
              >
                <Dropdown
                  id="swarm-start-profile"
                  ariaLabel={t('swarm.profile')}
                  value={model.selectedProfile?.id ?? ''}
                  options={model.profiles.map((profile) => ({
                    value: profile.id,
                    label: profile.name,
                  }))}
                  onValueChange={(id) =>
                    model.selectRunProfile(
                      model.profiles.find((profile) => profile.id === id),
                    )}
                />
              </FormField>
              <Button
                variant="tertiary"
                icon
                ariaLabel={t('common.edit')}
                tooltip={t('swarm.editProfile')}
                disabled={!model.selectedProfile}
                onClick={() => model.openProfile(model.selectedProfile)}
                >{@render actionIcon('edit')}</Button
              >
              <Button
                variant="tertiary"
                icon
                ariaLabel={t('common.delete')}
                tooltip={t('swarm.delete.title')}
                disabled={!model.selectedProfile}
                onClick={() =>
                  model.confirmProfileDelete(model.selectedProfile)}
                >{@render actionIcon('trash')}</Button
              >
            </div>
            <FormField
              controlId="swarm-start-directory"
              label={t('swarm.profile.directoryHeading')}
            >
              <TextField
                id="swarm-start-directory"
                code
                value={model.runDirectory}
                disabled={!model.selectedProfile ||
                  model.directoryLoading ||
                  model.pending === 'start'}
                onInput={(value) => (model.runDirectory = value)}
              />
            </FormField>
            <FormField controlId="swarm-goal" label={t('swarm.goal')}>
              <TextArea
                id="swarm-goal"
                value={model.goal}
                onInput={(value) => (model.goal = value)}
                rows="6"
                placeholder={t('swarm.goalPlaceholder')}
              />
            </FormField><Button
              variant="primary"
              loading={model.pending === 'start'}
              disabled={!model.selectedProfile ||
                model.directoryLoading ||
                !model.runDirectory.trim()}
              onClick={model.startSwarm}
              >{@render actionIcon('play')}{model.pending === 'start'
                ? t('swarm.starting')
                : t('swarm.startButton')}</Button
            >
          </section>{/if}
      </section>
    {/if}
  </div>
</main>

{#if model.composeOpen && model.selectedSwarm}<Modal
    title={t('swarm.board.openComposer')}
    closeDisabled={model.posting}
    onClose={() => (model.composeOpen = false)}
  >
    {#snippet body()}<div class="modal-copy swarm-page-modal-copy">
        {#if model.error}<Banner variant="error" role="alert"
            >{model.error}</Banner
          >{/if}
        <FormField controlId="swarm-post" label={t('swarm.board.post')}
          ><textarea
            class="text-area text-area--default"
            id="swarm-post"
            bind:value={model.postText}
            rows="3"
            placeholder={t('swarm.board.placeholder')}></textarea></FormField
        >
        <div class="post-options">
          <FormField controlId="swarm-reply" label={t('swarm.board.replyTo')}
            ><input
              class="s-input"
              id="swarm-reply"
              placeholder="#42"
              bind:value={model.replyTo}
            /></FormField
          ><FormField
            controlId="swarm-pings"
            label={t('swarm.board.recipients')}
            ><input
              class="s-input"
              id="swarm-pings"
              bind:value={model.postRecipients}
            /></FormField
          >
        </div>
      </div>{/snippet}
    {#snippet footer()}<Button
        variant="secondary"
        disabled={model.posting}
        onClick={() => (model.composeOpen = false)}>{t('common.cancel')}</Button
      ><Button
        variant="primary"
        loading={model.posting}
        disabled={!model.postText.trim()}
        onClick={model.post}>{t('swarm.board.submit')}</Button
      >{/snippet}
  </Modal>{/if}
{#if model.swarmDeleteCandidate}<Modal
    title={t('swarm.deleteRun.title')}
    closeDisabled={model.pending === 'delete'}
    onClose={() => (model.swarmDeleteCandidate = null)}
    >{#snippet body()}<div class="modal-copy swarm-page-modal-copy">
        <p>{model.swarmDeleteCandidate.prompt}</p>
        <p>
          {t('swarm.deleteRun.body')}
        </p>
        {#if model.deleteError}<Banner variant="error"
            >{model.deleteError}</Banner
          >{/if}
      </div>{/snippet}{#snippet footer()}<Button
        variant="secondary"
        disabled={model.pending === 'delete'}
        onClick={() => (model.swarmDeleteCandidate = null)}
        >{t('common.cancel')}</Button
      ><Button
        variant="danger"
        loading={model.pending === 'delete'}
        onClick={model.deleteSwarm}>{t('common.delete')}</Button
      >{/snippet}</Modal
  >{/if}
{#if model.deleteCandidate}<Modal
    title={t('swarm.delete.title')}
    closeDisabled={model.pending === 'delete'}
    onClose={() => (model.deleteCandidate = null)}
    >{#snippet body()}<div class="modal-copy swarm-page-modal-copy">
        <p>
          {t('swarm.delete.body', { name: model.deleteCandidate.name })}
        </p>
        {#if model.deleteError}<Banner variant="error"
            >{model.deleteError}</Banner
          >{/if}
      </div>{/snippet}{#snippet footer()}<Button
        variant="secondary"
        disabled={model.pending === 'delete'}
        onClick={() => (model.deleteCandidate = null)}
        >{t('common.cancel')}</Button
      ><Button
        variant="danger"
        loading={model.pending === 'delete'}
        onClick={model.deleteProfile}>{t('common.delete')}</Button
      >{/snippet}</Modal
  >{/if}
{#if model.settingsOpen}<Modal
    title={t('swarm.communication.title')}
    closeDisabled={model.pending === 'settings'}
    onClose={() => (model.settingsOpen = false)}
    >{#snippet body()}<div class="communication swarm-page-communication">
        {#each ['main', 'discussion', 'ping'] as route (route)}<section>
            <h3>{routeLabels[route]}</h3>
            <FormField
              controlId={`live-delivery-${route}`}
              label={t('swarm.delivery.mode')}
              ><select
                class="s-input"
                value={model.deliveryDraft[route].mode}
                onchange={(event) =>
                  model.changeDelivery(
                    route,
                    'mode',
                    event.currentTarget.value,
                  )}
                ><option value="all">{t('swarm.delivery.all')}</option><option
                  value="idle">{t('swarm.delivery.idle')}</option
                ><option value="pull">{t('swarm.delivery.pull')}</option
                ></select
              ></FormField
            ><label class="check"
              ><input
                type="checkbox"
                checked={model.deliveryDraft[route].wake_idle}
                onchange={(event) =>
                  model.changeDelivery(
                    route,
                    'wake_idle',
                    event.currentTarget.checked,
                  )}
              />
              {t('swarm.delivery.wake')}</label
            >
          </section>{/each}
        <section>
          <h3>{t('swarm.communication.advanced')}</h3>
          <div class="advanced-settings">
            <FormField
              controlId="live-coalesce"
              label={t('swarm.delivery.coalesce')}
              ><input
                class="s-input"
                id="live-coalesce"
                type="number"
                min="0"
                max="5000"
                value={model.deliveryDraft.coalesce_ms}
                onchange={(event) =>
                  model.changeDeliverySetting(
                    'coalesce_ms',
                    event.currentTarget.value,
                  )}
              /></FormField
            >
            <FormField
              controlId="live-batch-messages"
              label={t('swarm.delivery.batchMessages')}
              ><input
                class="s-input"
                id="live-batch-messages"
                type="number"
                min="1"
                max="100"
                value={model.deliveryDraft.batch_messages}
                onchange={(event) =>
                  model.changeDeliverySetting(
                    'batch_messages',
                    event.currentTarget.value,
                  )}
              /></FormField
            >
            <FormField
              controlId="live-batch-chars"
              label={t('swarm.delivery.batchChars')}
              ><input
                class="s-input"
                id="live-batch-chars"
                type="number"
                min="16000"
                max="128000"
                value={model.deliveryDraft.batch_chars}
                onchange={(event) =>
                  model.changeDeliverySetting(
                    'batch_chars',
                    event.currentTarget.value,
                  )}
              /></FormField
            >
          </div>
        </section>
        <section>
          <h3>{t('swarm.communication.proposed')}</h3>
          {#if model.settingChanges.length}<ul>
              {#each model.settingChanges as change (`${change.route}-${change.field}`)}<li
                >
                  {change.route} · {change.field}: <s>{change.before}</s> → {change.after}
                </li>{/each}
            </ul>{:else}<p>
              {t('swarm.communication.noChanges')}
            </p>{/if}
          <p>
            {t('swarm.communication.effect')}
          </p>
        </section>
      </div>{/snippet}{#snippet footer()}<Button
        variant="secondary"
        disabled={model.pending === 'settings'}
        onClick={() => (model.settingsOpen = false)}
        >{t('common.cancel')}</Button
      ><Button
        variant="primary"
        loading={model.pending === 'settings'}
        disabled={model.settingChanges.length === 0}
        onClick={model.applyDelivery}
        >{model.pending === 'settings'
          ? t('swarm.applying')
          : t('swarm.apply')}</Button
      >{/snippet}</Modal
  >{/if}
{#if model.profileSnapshotOpen}<Modal
    title={t('swarm.profileSnapshot')}
    onClose={() => (model.profileSnapshotOpen = false)}
    >{#snippet body()}<div class="modal-copy swarm-page-modal-copy">
        <pre>{JSON.stringify(
            model.selectedSwarm.profile_snapshot ??
              model.selectedSwarm.profile ??
              {},
            null,
            2,
          )}</pre>
      </div>{/snippet}{#snippet footer()}<Button
        variant="primary"
        onClick={() => (model.profileSnapshotOpen = false)}
        >{t('common.close')}</Button
      >{/snippet}</Modal
  >{/if}
