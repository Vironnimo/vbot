<script>
  import { participantColor } from './pagePresentation.js';
  import { activeLocaleTag, t } from '../../../../webui/src/lib/i18n.js';
  import { formatMoment } from '../../../../webui/src/lib/timeText.js';
  import {
    dateKeyInApplicationZone,
    formatDateTimeInApplicationZone,
  } from '../../../../webui/src/lib/dateTimePrefs.svelte.js';
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

  // Marks where a link goes in a translated sentence.
  const GAP = '\u0000';

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
  // What an empty Board waits for (participants still at work, or a Resume),
  // or why it stays empty.
  let emptyHint = $derived.by(() => {
    if (
      ['preparing', 'running'].includes(swarm.state) ||
      swarm.participants?.some((participant) => participant.run_active)
    )
      return t('swarm.board.emptyWorking');
    if (swarm.state === 'needs_attention')
      return t('swarm.board.emptyAttention');
    if (swarm.state === 'interrupted') return t('swarm.board.emptyInterrupted');
    if (['stopped', 'cancelled'].includes(swarm.state))
      return t('swarm.board.emptyStopped');
    return '';
  });

  // A post's time: the time for a post from today, the date for an older one
  // (with its year outside the current one). The tooltip gives the moment.
  function postTime(value) {
    const day = value ? dateKeyInApplicationZone(value) : '';
    if (!day) return '';
    const today = dateKeyInApplicationZone(Date.now());
    return formatDateTimeInApplicationZone(
      value,
      activeLocaleTag(),
      day === today
        ? { hour: 'numeric', minute: '2-digit' }
        : {
            month: 'short',
            day: 'numeric',
            ...(day.slice(0, 4) === today.slice(0, 4)
              ? {}
              : { year: 'numeric' }),
          },
    );
  }

  const authorName = (post) =>
    post.author?.kind === 'user'
      ? t('swarm.board.you')
      : (post.author_name ?? post.author?.name ?? t('swarm.participant'));

  let names = null;
  let namesLocale = '';
  // The participants a post addresses, by name.
  function recipientNames(post) {
    const locale = activeLocaleTag();
    if (namesLocale !== locale) {
      names = new Intl.ListFormat(locale, { type: 'conjunction' });
      namesLocale = locale;
    }
    return names.format(
      post.recipients.map(
        (id) =>
          swarm.participants?.find((participant) => participant.id === id)
            ?.display_name ?? id,
      ),
    );
  }

  // A post's own text opens when focus reaches into it, such as a link in
  // its folded part, or reaches the post itself, as a revealed post's does.
  function focusPost(event, post) {
    if (
      board.folds(post.id) &&
      (event.target.matches('[data-post-number]') ||
        event.target.closest('.post-body'))
    )
      board.expand(post.id);
  }
</script>

{#snippet replyIcon()}<svg
    class="post-icon"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    ><path d="m9 17-5-5 5-5" /><path d="M20 18v-2a4 4 0 0 0-4-4H4" /></svg
  >{/snippet}

{#snippet postTimeTag(post)}{#if post.created_at}<time
      datetime={post.created_at}
      use:tooltip={() => formatMoment(post.created_at)}
      >{postTime(post.created_at)}</time
    >{/if}{/snippet}

<div
  class="panel board-panel"
  role="tabpanel"
  id="swarm-run-panel-board"
  aria-labelledby="swarm-run-tab-board"
  use:board.panel
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
      onFooterAction={board.loadMoreDiscussions}
      onValueChange={(value) => board.chooseDiscussion(value)}
    />
    <Button
      variant="secondary"
      class="board-write"
      onClick={() => board.compose()}
    >
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
      {#each board.posts as post (post.id)}{#if post.discussion_announcement}
          {@const opened = t('swarm.board.discussionOpened', {
            name: authorName(post),
            title: GAP,
          }).split(GAP)}
          <li
            class="board-event discussion-announcement"
            data-post-number={post.sequence}
          >
            <ParticipantAvatar
              id={post.author?.id}
              name={post.author?.name}
              kind={post.author?.kind}
            />
            <p>
              {opened[0]}<a
                href="#discussion/{encodeURIComponent(
                  post.discussion_announcement.discussion_id,
                )}">{post.discussion_announcement.title}</a
              >{opened[1]}
            </p>
            {@render postTimeTag(post)}
          </li>
        {:else}
          <li
            class="board-post"
            data-post-number={post.sequence}
            style:--participant-color={post.author?.kind === 'participant' &&
            post.author?.id
              ? participantColor(post.author.id)
              : 'var(--text-lo)'}
            onfocusin={(event) => focusPost(event, post)}
          >
            <div class="post-header">
              <ParticipantAvatar
                id={post.author?.id}
                name={post.author?.name}
                kind={post.author?.kind}
              />
              <span class="post-author">{authorName(post)}</span>
              {#if post.sequence != null}<span class="post-number"
                  >#{post.sequence}</span
                >{/if}
              {@render postTimeTag(post)}
              {#if post.sequence != null}<Button
                  variant="tertiary"
                  icon
                  class="post-reply"
                  ariaLabel={t('swarm.board.replyTo', {
                    reference: `#${post.sequence}`,
                  })}
                  tooltip={t('swarm.board.reply')}
                  onClick={() => board.compose(post)}
                  >{@render replyIcon()}</Button
                >{/if}
            </div>
            {#if post.reply_sequence != null || post.recipients?.length}
              <p class="post-context">
                {#if post.reply_sequence != null}
                  {@const reply = t('swarm.board.inReplyTo', {
                    reference: GAP,
                  }).split(GAP)}
                  {@render replyIcon()}<span
                    >{reply[0]}<a href="#post/{post.reply_sequence}"
                      >#{post.reply_sequence}</a
                    >{reply[1]}</span
                  >
                {/if}
                {#if post.recipients?.length}{#if post.reply_sequence != null}<span
                      class="post-context__gap"
                      aria-hidden="true">·</span
                    >{/if}<span
                    >{t('swarm.board.addressed', {
                      names: recipientNames(post),
                    })}</span
                  >{/if}
              </p>
            {/if}
            <div
              class="post-body"
              class:post-body--folded={board.folds(post.id) &&
                !board.opened(post.id)}
              use:board.postBody={post.id}
            >
              <MarkdownContent
                source={post.text}
                class="msg-markdown md-chat"
              />
            </div>
            {#if board.folds(post.id)}<Button
                variant="tertiary"
                class="post-fold"
                aria-expanded={board.opened(post.id)}
                onClick={(event) =>
                  board.toggleExpanded(post.id, event.currentTarget)}
                >{board.opened(post.id)
                  ? t('swarm.board.showLess')
                  : t('swarm.board.showMore')}</Button
              >{/if}
          </li>
        {/if}{/each}
    </ol>
    {#if board.boardCursor}<Button
        variant="secondary"
        onClick={board.loadEarlierPosts}>{t('swarm.board.more')}</Button
      >{/if}{/if}
</div>
