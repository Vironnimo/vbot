<script>
  import { participantColor } from './pagePresentation.js';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import CopyButton from '../../../../webui/src/components/ui/CopyButton.svelte';
  import EmptyState from '../../../../webui/src/components/ui/EmptyState.svelte';
  import SearchableDropdown from '../../../../webui/src/components/SearchableDropdown.svelte';
  import { tooltip } from '../../../../webui/src/lib/tooltip.js';
  import MarkdownContent from '../../../../webui/src/components/chat/MarkdownContent.svelte';
  import SwarmIcon from './SwarmIcon.svelte';
  import ParticipantAvatar from './ParticipantAvatar.svelte';
  import ParticipantChip from './ParticipantChip.svelte';
  import './runBoard.css';

  // The Board tab: the discussion picker, the Run's goal and working
  // directory, the discussion's participants and its posts. Posts stay `li`
  // children of `ol.board`, which carries the content links, inside the
  // page's scroller.
  let { board, model } = $props();

  let swarm = $derived(model.selectedSwarm);
  let directory = $derived(swarm.effective_configuration?.cwd ?? '');
  // The directory's last segment stays whole; the path before it gives way.
  let directoryParts = $derived.by(() => {
    const cut =
      Math.max(
        directory.lastIndexOf('/', directory.length - 2),
        directory.lastIndexOf('\\', directory.length - 2),
      ) + 1;
    return { head: directory.slice(0, cut), tail: directory.slice(cut) };
  });
  // The goal's first line without Markdown markers, shown while it is folded.
  let goalPreview = $derived(
    (swarm.prompt ?? '')
      .split(/\r?\n/)
      .map((line) => line.replace(/^\s*(?:#+|>|[-*+]|\d+[.)])\s+/, '').trim())
      .find(Boolean) ?? '',
  );
  // What an empty Board waits for: participants still at work, or a Resume.
  let emptyHint = $derived(
    ['preparing', 'running'].includes(swarm.state) ||
      swarm.participants?.some((participant) => participant.run_active)
      ? t('swarm.board.emptyWorking')
      : swarm.state === 'needs_attention'
        ? t('swarm.board.emptyAttention')
        : '',
  );
</script>

<section
  class="panel board-panel"
  role="tabpanel"
  id="swarm-run-panel-board"
  aria-labelledby="swarm-run-tab-board"
>
  <div class="board-toolbar">
    <SearchableDropdown
      id="swarm-discussion"
      value={board.selectedDiscussion}
      options={board.discussionOptions}
      ariaLabel={t('swarm.board.discussionPicker', {
        title: board.discussionTitle,
      })}
      triggerClass="board-discussion"
      footerActionLabel={board.discussionCursor
        ? t('swarm.board.moreDiscussions')
        : ''}
      onFooterAction={() =>
        board.loadDiscussions(swarm, board.discussionCursor)}
      onValueChange={(value) => board.chooseDiscussion(value)}
    />
    <Button variant="secondary" onClick={() => (board.composeOpen = true)}>
      <SwarmIcon kind="edit" />{t('swarm.board.openComposer')}
    </Button>
  </div>
  <div class="board-goal">
    <details
      class="swarm-goal-post"
      use:board.contentLinks={{ references: false }}
    >
      <summary
        ><span class="board-goal__title">{t('swarm.board.goal')}</span
        >{#if goalPreview}<span class="board-goal__preview">{goalPreview}</span
          >{/if}</summary
      >
      <MarkdownContent source={swarm.prompt} class="msg-markdown md-chat" />
    </details>
    {#if directory}
      <div class="board-directory">
        <SwarmIcon kind="folder" />
        <span
          class="board-directory__path"
          use:tooltip={{
            title: t('swarm.board.directory'),
            text: directory,
            mono: true,
            selectable: true,
          }}
          ><span class="board-directory__head">{directoryParts.head}</span><span
            class="board-directory__tail">{directoryParts.tail}</span
          ></span
        >
        <CopyButton
          text={directory}
          label={t('swarm.board.copyDirectory')}
          class="board-directory__copy"
        />
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
  {#if board.posts.length === 0}{#if board.loaded}<EmptyState
        class="board-empty"
        title={t('swarm.board.empty')}
        description={emptyHint}
      />{/if}{:else}{#if !board.boardAtNewest}<div class="board-newer">
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
