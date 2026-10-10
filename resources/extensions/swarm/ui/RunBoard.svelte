<script>
  import { participantColor } from './pagePresentation.js';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import EmptyState from '../../../../webui/src/components/ui/EmptyState.svelte';
  import { tooltip } from '../../../../webui/src/lib/tooltip.js';
  import FormField from '../../../../webui/src/components/ui/FormField.svelte';
  import MarkdownContent from '../../../../webui/src/components/chat/MarkdownContent.svelte';
  import SwarmIcon from './SwarmIcon.svelte';
  import ParticipantAvatar from './ParticipantAvatar.svelte';
  import ParticipantChip from './ParticipantChip.svelte';

  // The Board tab: the discussion picker, the Run's goal and directory, the
  // discussion's participants and its posts. Posts stay `li` children of
  // `ol.board`, which carries the content links, inside the page's scroller.
  let { board, model } = $props();
</script>

<section class="panel board-panel" role="tabpanel">
  <div class="section-head board-toolbar">
    <FormField controlId="swarm-discussion" label={t('swarm.board.discussion')}
      ><select
        class="s-input"
        id="swarm-discussion"
        value={board.selectedDiscussion}
        onchange={(event) => board.chooseDiscussion(event.currentTarget.value)}
        >{#each board.discussionOptions as discussion (discussion.id)}<option
            value={discussion.id}
            >{discussion.title ?? discussion.name ?? discussion.id}</option
          >{/each}</select
      ></FormField
    >
    {#if board.discussionCursor}<Button
        variant="secondary"
        onClick={() =>
          board.loadDiscussions(model.selectedSwarm, board.discussionCursor)}
        >{t('swarm.board.moreDiscussions')}</Button
      >{/if}
    <Button variant="secondary" onClick={() => (board.composeOpen = true)}>
      <SwarmIcon kind="edit" />{t('swarm.board.openComposer')}
    </Button>
  </div>
  <div class="board-context">
    <details
      class="swarm-goal-post"
      use:board.contentLinks={{ references: false }}
    >
      <summary>{t('swarm.board.goal')}</summary>
      <MarkdownContent
        source={model.selectedSwarm.prompt}
        class="msg-markdown md-chat"
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
        <SwarmIcon kind="folder" />
        <span>{model.selectedSwarm.effective_configuration.cwd}</span>
      </div>
    {/if}
  </div>
  <section class="participant-pane" aria-label={t('swarm.participants')}>
    <p class="section-label">
      {t('swarm.participants')}
      <span class="participant-count"
        >{board.discussionParticipants.length}</span
      >
    </p>
    <div class="participant-row">
      {#each board.discussionParticipants as participant (participant.id)}
        <ParticipantChip
          {participant}
          onInspect={model.activity.inspectParticipant}
        />
      {:else}
        <span class="muted">{t('swarm.board.noParticipants')}</span>
      {/each}
    </div>
  </section>
  {#if board.posts.length === 0}<EmptyState
      density="compact"
      title={t('swarm.board.empty')}
    />{:else}{#if !board.boardAtNewest}<div class="board-newer">
        <Button variant="secondary" onClick={board.loadNewerPosts}
          >{t('swarm.board.newer')}</Button
        >
        <Button variant="secondary" onClick={board.jumpToNewest}
          >{t('swarm.board.newest')}</Button
        >
      </div>{/if}
    <ol class="board" use:board.contentLinks>
      {#each board.posts as post (post.id)}<li
          data-post-number={post.sequence}
          style:--participant-color={post.author?.kind === 'participant' &&
          post.author?.id
            ? participantColor(post.author.id)
            : 'var(--text-med)'}
        >
          <div class="post-header">
            <div class="post-author">
              <ParticipantAvatar
                id={post.author?.id}
                name={post.author?.name}
                kind={post.author?.kind}
              />
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
                  board.openDiscussion(post.discussion_announcement)}
              >
                {post.discussion_announcement.title}
              </Button>
            </div>
          {:else}<MarkdownContent
              source={post.text}
              class="msg-markdown md-chat"
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
    {#if board.boardCursor}<Button
        variant="secondary"
        onClick={() =>
          board.loadBoard(
            model.selectedSwarm,
            board.selectedDiscussion,
            board.boardCursor,
          )}>{t('swarm.board.more')}</Button
      >{/if}{/if}
</section>
