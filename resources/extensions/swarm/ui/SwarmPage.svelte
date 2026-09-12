<script>
  import {
    participantColor,
    participantInitials,
    canStop,
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
  import { createSwarmPageModel } from './pageModel.svelte.js';
  import { createSwarmPageActivity } from './pageActivity.svelte.js';
  import './swarmPage.css';

  let { bridgeClient = null } = $props();
  const model = createSwarmPageModel({
    get bridgeClient() {
      return bridgeClient;
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
      <span class="secondary-pane__title">{t('swarm.profiles', 'Swarms')}</span>
      <Button
        variant="tertiary"
        icon
        ariaLabel={t('swarm.newProfile', 'New Swarm')}
        tooltip={t('swarm.newProfile', 'New Swarm')}
        loading={model.pending === 'profile'}
        onClick={() => model.navigate(() => model.openProfile())}
        >{@render actionIcon('plus')}</Button
      >
    </div>
    <div class="secondary-pane__scroll">
      <nav class="secondary-list" aria-label={t('swarm.profiles', 'Swarms')}>
        {#each model.profiles as profile (profile.id)}
          <button
            class="secondary-list__item"
            class:active={model.editor?.id === profile.id}
            aria-current={model.editor?.id === profile.id ? 'page' : undefined}
            onclick={() => model.navigate(() => model.openProfile(profile))}
          >
            <span class="sidebar-title"
              >{profile.name} ({(profile.participants ?? []).reduce(
                (sum, row) => sum + row.count,
                0,
              )})</span
            >
          </button>
        {:else}<EmptyState
            density="compact"
            title={t('swarm.noProfiles', 'No Swarms yet.')}
            description={t(
              'swarm.noProfilesHelp',
              'Create a Swarm to start a Run.',
            )}
          />{/each}
        {#if model.profilesCursor}<Button
            variant="tertiary"
            onClick={model.loadMoreProfiles}
            >{t('swarm.profiles.more', 'Load more Swarms')}</Button
          >{/if}
      </nav>
      {#each model.runGroups as group (group.id)}
        <section class="run-group">
          <div class="secondary-pane__header swarms-head">
            <span class="secondary-pane__title">{group.label}</span>
          </div>
          <nav class="secondary-list" aria-label={group.label}>
            {#each group.entries as swarm (swarm.id)}
              <button
                class="secondary-list__item"
                class:active={!model.editor &&
                  model.selectedSwarm?.id === swarm.id}
                use:tooltip={swarm.title || swarm.id}
                onclick={() =>
                  model.navigate(() => model.selectSwarm(swarm.id))}
              >
                <span class="sidebar-title"
                  >{swarm.title ||
                    swarm.prompt?.split(/\r?\n/)[0] ||
                    swarm.id}</span
                >
              </button>
            {/each}
          </nav>
        </section>
      {/each}
      {#if model.swarmsCursor}<Button
          variant="tertiary"
          onClick={model.loadMoreSwarms}
          >{t('swarm.swarms.more', 'Load more runs')}</Button
        >{/if}
    </div>
    <div class="sidebar-footer">
      <Button variant="secondary" onClick={model.newSwarm}
        >{@render actionIcon('play')}{t('swarm.newRun', 'New run')}</Button
      >
    </div>
  </aside>
  <div class="workspace">
    {#if model.error}<Banner variant="error" role="alert">{model.error}</Banner
      >{/if}
    {#if model.loading}<Banner variant="info" role="status"
        >{t('swarm.loading', 'Loading Swarms…')}</Banner
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
            disabled={model.loading}
            onClick={() => model.refresh()}
            >{@render actionIcon('refresh')}</Button
          >
        </div>
        {#if model.selectedSwarm}<div class="swarm-head">
            <div>
              <h2>{t('swarm.userPrompt', 'User Prompt:')}</h2>
              <p class="goal">{model.selectedSwarm.prompt}</p>
              <p class="muted">
                {model.selectedSwarm.effective_configuration?.cwd ?? ''}
              </p>
            </div>
            <div class="actions">
              {#if canStop(model.selectedSwarm.state)}<Button
                  variant="danger"
                  loading={model.pending === 'stop'}
                  onClick={() => model.lifecycle('stop')}
                  >{@render actionIcon('stop')}{model.pending === 'stop'
                    ? t('swarm.stopping', 'Stopping...')
                    : t('swarm.stop', 'Stop')}</Button
                >{/if}{#if model.canResume}<Button
                  variant="primary"
                  loading={model.pending === 'resume'}
                  disabled={model.pending === 'stop'}
                  onClick={() => model.lifecycle('resume')}
                  >{@render actionIcon('play')}{model.pending === 'resume'
                    ? t('swarm.resuming', 'Resuming...')
                    : t('swarm.resume', 'Resume')}</Button
                >{/if}<Button
                variant="danger"
                disabled={!!model.pending || canStop(model.selectedSwarm.state)}
                tooltip={canStop(model.selectedSwarm.state)
                  ? t(
                      'swarm.deleteRun.stopFirst',
                      'Stop the Swarm before deleting it.',
                    )
                  : t('swarm.deleteRun.title', 'Delete Run')}
                onClick={() => {
                  model.deleteError = '';
                  model.swarmDeleteCandidate = model.selectedSwarm;
                }}>{t('swarm.deleteRun.title', 'Delete Run')}</Button
              ><Button
                variant="secondary"
                onClick={() => (model.profileSnapshotOpen = true)}
                icon
                ariaLabel={t('swarm.profileSnapshot', 'Inspect Swarm snapshot')}
                tooltip={t('swarm.profileSnapshot', 'Inspect Swarm snapshot')}
                >{@render actionIcon('document')}</Button
              ><Button
                variant="tertiary"
                icon
                onClick={model.openDelivery}
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
              items={model.tabs}
              value={model.activeTab}
              ariaLabel={t('swarm.details', 'Swarm details')}
              onChange={(next) => (model.activeTab = next)}
            /><StatusChip
              variant={canStop(model.selectedSwarm.state) ? 'warn' : 'neutral'}
              >{t(
                `swarm.state.${model.selectedSwarm.state}`,
                model.selectedSwarm.state,
              )}</StatusChip
            >
          </div>
          {#if model.activeTab === 'board'}<section
              class="panel"
              role="tabpanel"
            >
              <div class="section-head">
                <h3>{t('swarm.board.title', 'Board')}</h3>
                <Button
                  variant="secondary"
                  onClick={() => (model.composeOpen = true)}
                  >{t('swarm.board.openComposer', 'Write post')}</Button
                >
                <FormField
                  controlId="swarm-discussion"
                  label={t('swarm.board.discussion', 'Discussion')}
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
                      )}
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
                  {#each model.selectedSwarm.participants ?? [] as participant (participant.id)}<Button
                      variant="secondary"
                      onClick={() => activity.inspectParticipant(participant)}
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
              {#if model.board.length === 0}<EmptyState
                  density="compact"
                  title={t('swarm.board.empty', 'No Board messages yet.')}
                />{:else}<ol class="board" use:model.contentLinks>
                  {#each model.board as post (post.id)}<li>
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
                          >{model.date(post.created_at)}</time
                        >
                      </div>
                      {#if post.discussion_announcement}
                        <div class="discussion-announcement">
                          <p>
                            {t(
                              'swarm.board.discussionOpened',
                              '{name} opened a discussion.',
                              { name: post.author.name },
                            )}
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
                {#if model.boardCursor}<Button
                    variant="secondary"
                    onClick={() =>
                      model.loadBoard(
                        model.selectedSwarm,
                        model.selectedDiscussion,
                        model.boardCursor,
                      )}
                    >{t('swarm.board.more', 'Load earlier messages')}</Button
                  >{/if}{/if}
            </section>
          {:else if model.activeTab === 'participants'}<section
              class="panel"
              role="tabpanel"
            >
              <h3>{t('swarm.participants', 'Participants')}</h3>
              <div class="participants">
                {#each model.selectedSwarm.participants ?? [] as participant (participant.id)}<Button
                    variant="secondary"
                    aria-pressed={activity.history?.participant.id ===
                      participant.id}
                    onClick={() => activity.inspectParticipant(participant)}
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
              {#if activity.history}<article
                  class="history"
                  use:model.contentLinks
                >
                  <div class="section-head">
                    <h3>
                      {activity.history.participant.display_name}
                      {t('swarm.activity', 'activity')}
                    </h3>
                    <div class="actions">
                      <button
                        class="context-usage"
                        aria-label={t(
                          'chat.contextRingLabel',
                          'Context window usage',
                        )}
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
                        {t('swarm.context', 'Context')}: {activity.contextTokens}
                      </button>
                      {#if model.canResume && activity.selectedParticipant && !activity.selectedParticipant.run_active && resumableParticipantState(activity.selectedParticipant.state)}
                        <Button
                          variant="primary"
                          disabled={Boolean(model.pending)}
                          onClick={() =>
                            model.lifecycle(
                              'resume',
                              activity.selectedParticipant.id,
                            )}
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
                      {t('chat.loadOlderMessages', 'Load older messages')}
                    </Button>
                  {/if}
                  {#each activity.activityTimeline as item (item.id)}
                    {#if item.type === 'assistant_run'}<ChatAssistantRun
                        {item}
                        agentName={activity.history.participant.display_name}
                      />{:else}<ChatTimelineEntry
                        {item}
                        agentName={activity.history.participant.display_name}
                        messageEditingDisabled
                      />{/if}
                  {:else}<EmptyState
                      density="compact"
                      title={t('swarm.noActivity', 'No retained activity yet.')}
                    />{/each}
                </article>{/if}
            </section>
          {:else if model.activeTab === 'usage'}<section
              class="panel"
              role="tabpanel"
            >
              <h3>{t('swarm.usage', 'Usage')}</h3>
              <dl class="swarm-identity">
                <dt>{t('swarm.id', 'Swarm ID')}</dt>
                <dd>{model.selectedSwarm.id}</dd>
              </dl>
              {#if model.usage?.usage}<dl class="usage-summary">
                  <div>
                    <dt>
                      {t('swarm.usage.tokensUsed', 'Tokens used')}
                    </dt>
                    <dd>{tokensUsed(model.usage.usage.usage?.totals)}</dd>
                  </div>
                  <div>
                    <dt>{t('swarm.usage.toolCalls', 'Tool Calls')}</dt>
                    <dd>
                      {usageCount(model.usage.usage.tools?.total_calls)}
                    </dd>
                  </div>
                </dl>
                {#if model.usageRows.length}<div class="table-wrap">
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
                {#each model.events as event (event.id)}<li>
                    <strong>{event.kind}</strong><span
                      >{event.actor} / {model.date(event.created_at)}</span
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
              {#if model.eventsCursor}<Button
                  variant="secondary"
                  onClick={model.loadMoreEvents}
                  >{t('swarm.audit.more', 'Load more events')}</Button
                >{/if}
            </section>{/if}
        {:else}<section class="start">
            <p class="eyebrow">{t('swarm.start', 'Start')}</p>
            <h2>{t('swarm.startTitle', 'Give the group a goal')}</h2>
            <p>
              {t(
                'swarm.startHelp',
                'Every participant starts with this same goal and the selected Swarm configuration.',
              )}
            </p>
            <div class="start-profile">
              <FormField
                controlId="swarm-start-profile"
                label={t('swarm.profile', 'Swarm')}
              >
                <Dropdown
                  id="swarm-start-profile"
                  ariaLabel={t('swarm.profile', 'Swarm')}
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
                ariaLabel={t('common.edit', 'Edit')}
                tooltip={t('swarm.editProfile', 'Edit Swarm')}
                disabled={!model.selectedProfile}
                onClick={() => model.openProfile(model.selectedProfile)}
                >{@render actionIcon('edit')}</Button
              >
              <Button
                variant="tertiary"
                icon
                ariaLabel={t('common.delete', 'Delete')}
                tooltip={t('swarm.delete.title', 'Delete Swarm')}
                disabled={!model.selectedProfile}
                onClick={() => (model.deleteCandidate = model.selectedProfile)}
                >{@render actionIcon('trash')}</Button
              >
            </div>
            <FormField
              controlId="swarm-start-directory"
              label={t('swarm.profile.directoryHeading', 'Working directory')}
            >
              <TextField
                id="swarm-start-directory"
                value={model.runDirectory}
                disabled={!model.selectedProfile ||
                  model.directoryLoading ||
                  model.pending === 'start'}
                onInput={(value) => (model.runDirectory = value)}
              />
            </FormField>
            <FormField controlId="swarm-goal" label={t('swarm.goal', 'Goal')}>
              <TextArea
                id="swarm-goal"
                value={model.goal}
                onInput={(value) => (model.goal = value)}
                rows="6"
                placeholder={t(
                  'swarm.goalPlaceholder',
                  'Describe the work the group should do',
                )}
              />
            </FormField><Button
              variant="primary"
              loading={model.pending === 'start'}
              disabled={!model.selectedProfile ||
                model.directoryLoading ||
                !model.runDirectory.trim()}
              onClick={model.startSwarm}
              >{@render actionIcon('play')}{model.pending === 'start'
                ? t('swarm.starting', 'Starting…')
                : t('swarm.startButton', 'Start Run')}</Button
            >
          </section>{/if}
      </section>
    {/if}
  </div>
</main>

{#if model.composeOpen && model.selectedSwarm}<Modal
    title={t('swarm.board.openComposer', 'Write post')}
    closeDisabled={model.posting}
    onClose={() => (model.composeOpen = false)}
  >
    {#snippet body()}<div class="modal-copy swarm-page-modal-copy">
        {#if model.error}<Banner variant="error" role="alert"
            >{model.error}</Banner
          >{/if}
        <FormField
          controlId="swarm-post"
          label={t('swarm.board.post', 'Post to the Board')}
          ><textarea
            class="text-area text-area--default"
            id="swarm-post"
            bind:value={model.postText}
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
              bind:value={model.replyTo}
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
              bind:value={model.postRecipients}
            /></FormField
          >
        </div>
      </div>{/snippet}
    {#snippet footer()}<Button
        variant="secondary"
        disabled={model.posting}
        onClick={() => (model.composeOpen = false)}
        >{t('common.cancel', 'Cancel')}</Button
      ><Button
        variant="primary"
        loading={model.posting}
        disabled={!model.postText.trim()}
        onClick={model.post}>{t('swarm.board.submit', 'Post')}</Button
      >{/snippet}
  </Modal>{/if}
{#if model.swarmDeleteCandidate}<Modal
    title={t('swarm.deleteRun.title', 'Delete Run')}
    closeDisabled={model.pending === 'delete'}
    onClose={() => (model.swarmDeleteCandidate = null)}
    >{#snippet body()}<div class="modal-copy swarm-page-modal-copy">
        <p>{model.swarmDeleteCandidate.prompt}</p>
        <p>
          {t(
            'swarm.deleteRun.body',
            'Permanently delete this Run, its Board and participant Sessions? The Swarm will be kept. This cannot be undone.',
          )}
        </p>
        {#if model.deleteError}<Banner variant="error"
            >{model.deleteError}</Banner
          >{/if}
      </div>{/snippet}{#snippet footer()}<Button
        variant="secondary"
        disabled={model.pending === 'delete'}
        onClick={() => (model.swarmDeleteCandidate = null)}
        >{t('common.cancel', 'Cancel')}</Button
      ><Button
        variant="danger"
        loading={model.pending === 'delete'}
        onClick={model.deleteSwarm}>{t('common.delete', 'Delete')}</Button
      >{/snippet}</Modal
  >{/if}
{#if model.deleteCandidate}<Modal
    title={t('swarm.delete.title', 'Delete Swarm')}
    onClose={() => (model.deleteCandidate = null)}
    >{#snippet body()}<div class="modal-copy swarm-page-modal-copy">
        <p>
          {t(
            'swarm.delete.body',
            'Delete {name}? Existing Runs remain available.',
            { name: model.deleteCandidate.name },
          )}
        </p>
      </div>{/snippet}{#snippet footer()}<Button
        variant="secondary"
        onClick={() => (model.deleteCandidate = null)}
        >{t('common.cancel', 'Cancel')}</Button
      ><Button variant="danger" onClick={model.deleteProfile}
        >{t('common.delete', 'Delete')}</Button
      >{/snippet}</Modal
  >{/if}
{#if model.settingsOpen}<Modal
    title={t('swarm.communication.title', 'Change communication settings')}
    closeDisabled={model.pending === 'settings'}
    onClose={() => (model.settingsOpen = false)}
    >{#snippet body()}<div class="communication swarm-page-communication">
        {#each ['main', 'discussion', 'ping'] as route (route)}<section>
            <h3>{t(`swarm.delivery.${route}`, route)}</h3>
            <FormField
              controlId={`live-delivery-${route}`}
              label={t('swarm.delivery.mode', 'Mode')}
              ><select
                class="s-input"
                value={model.deliveryDraft[route].mode}
                onchange={(event) =>
                  model.changeDelivery(
                    route,
                    'mode',
                    event.currentTarget.value,
                  )}
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
                checked={model.deliveryDraft[route].wake_idle}
                onchange={(event) =>
                  model.changeDelivery(
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
              label={t('swarm.delivery.batchMessages', 'Messages per batch')}
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
              label={t('swarm.delivery.batchChars', 'Characters per batch')}
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
          <h3>{t('swarm.communication.proposed', 'Proposed changes')}</h3>
          {#if model.settingChanges.length}<ul>
              {#each model.settingChanges as change (`${change.route}-${change.field}`)}<li
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
        disabled={model.pending === 'settings'}
        onClick={() => (model.settingsOpen = false)}
        >{t('common.cancel', 'Cancel')}</Button
      ><Button
        variant="primary"
        loading={model.pending === 'settings'}
        disabled={model.settingChanges.length === 0}
        onClick={model.applyDelivery}
        >{model.pending === 'settings'
          ? t('swarm.applying', 'Applying…')
          : t('swarm.apply', 'Apply changes')}</Button
      >{/snippet}</Modal
  >{/if}
{#if model.profileSnapshotOpen}<Modal
    title={t('swarm.profileSnapshot', 'Swarm snapshot')}
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
        >{t('common.close', 'Close')}</Button
      >{/snippet}</Modal
  >{/if}
