import { SvelteMap } from 'svelte/reactivity';
import { tick } from 'svelte';
import { t } from '../../../../webui/src/lib/i18n.js';
import { page, participantCard, requestId } from './pagePresentation.js';
import { failureText } from './failureText.js';
import { swarmChanged } from './pageRefresh.js';
import { referenceLinks } from './referenceLinks.js';

// Posts per Board read: the newest page, an earlier or newer page, new posts,
// and each side of a revealed post. The Store also caps a page's text.
const BOARD_PAGE = 50;

// The most animation frames a revealed post is kept in place after a jump.
const RECENTER_FRAMES = 12;

// Input by which the reader takes over the scrolling from a reveal.
const READER_INPUT = ['wheel', 'touchstart', 'keydown', 'pointerdown'];

// A post longer than this many lines folds to its first lines (see the CSS)
// until the reader opens it; a shorter one shows in full, so Show more never
// reveals just a line or two.
const COLLAPSIBLE_LINES = 16;

// The selected Swarm's Board: the selected discussion's posts and their
// paging, the discussions, the post composer, and the links in rendered
// Markdown, which reveal cited posts on the Board, and the folding of long
// posts. `host` is the page model's side: `call`, `client`, the shown
// `selectedSwarm` and its `selectionRequest`, `disposed`, the page `error`,
// and the page steps `navigate`, `stepToBoard`, `openWikiPage`,
// `inspectParticipant`, `refresh` and `showResumeFailures`.
export function createSwarmPageBoard(host) {
  let posts = $state([]);

  let boardCursor = $state(null);

  let discussions = $state([]);

  let selectedDiscussion = $state('');

  let discussionCursor = $state(null);

  let postText = $state('');

  let composeOpen = $state(false);

  // The participants the post addresses, by participant id.
  let recipients = $state({});

  // The post the composer replies to: its id, number and tooltip.
  let replyTarget = $state(null);

  let posting = $state(false);
  let postMutation = null;

  // `posts` holds posts of `boardShown` (a Swarm and discussion), newest
  // first: its newest page with any earlier pages below, or the posts around a
  // revealed post with any earlier and newer pages. `boardAtNewest` tells
  // whether the discussion's newest posts are loaded; only then do new posts
  // go on top. Each full load (the newest page or a revealed post's
  // surroundings) counts up `boardVersion`; an earlier page or newer posts
  // read for an older version are dropped, since the newer full load
  // includes or replaces them.
  let boardShown = null;

  let boardAtNewest = $state(true);

  // Whether `posts` holds a loaded page, so an empty Board reads as empty
  // only once its posts arrived.
  let loaded = $state(false);

  let boardVersion = 0;

  // The full load in flight, which a read of new posts waits for.
  let boardLoading = null;

  // The requested newer page in flight, which a read of new posts waits for.
  let boardNewerLoading = null;

  let discussionsRequest = 0;

  let revealRequest = 0;

  // Board panels on screen (`panel`). Only the visible tab loads changes, so a
  // Board off screen may miss posts and discussions.
  let panels = 0;

  // Tooltip content for "#N" and "wN" links, by "post:N" and "page:N".
  const references = new SvelteMap();

  // By post id: whether a post folds, and whether the reader opened it.
  let folding = $state({});
  let opened = $state({});

  // Measures the post bodies as they are laid out (`postBody`).
  let bodies = null;
  const bodyPosts = new WeakMap();

  // The discussion picker's options: each discussion's title, the main one
  // named as such, with its number ("d2") beside it.
  const discussionOptions = $derived(
    discussions.map((discussion) => ({
      value: discussion.id,
      label:
        discussion.id === host.selectedSwarm?.main_discussion_id
          ? t('swarm.board.mainDiscussion')
          : (discussion.title ?? discussion.name ?? discussion.id),
      secondaryLabel:
        discussion.sequence != null ? `d${discussion.sequence}` : '',
    })),
  );
  const discussionTitle = $derived(
    discussionOptions.find((option) => option.value === selectedDiscussion)
      ?.label ?? '',
  );
  const discussionParticipants = $derived(
    (host.selectedSwarm?.participants ?? []).filter((participant) =>
      participant.discussion_ids?.includes(selectedDiscussion),
    ),
  );

  // Starts another Swarm's Board at its main discussion, nothing loaded yet.
  function reset(swarm) {
    references.clear();
    folding = {};
    opened = {};
    recipients = {};
    replyTarget = null;
    selectedDiscussion = swarm.main_discussion_id;
    discussions = [];
    clearBoard();
  }

  // Loads the Board, or with `changes` only its parts that changed: the
  // discussions, and the posts newer than those the Board shows.
  function reload(swarm, changes) {
    const changed = (resource) => swarmChanged(changes, swarm.id, resource);
    return Promise.all([
      changed('discussions') ? loadDiscussions(swarm) : null,
      !changed('posts')
        ? null
        : changes
          ? loadNewPosts(swarm)
          : loadBoard(swarm, selectedDiscussion),
    ]);
  }

  // Posts never change; a Wiki page's title and text can.
  function forgetPageReferences() {
    for (const key of references.keys())
      if (key.startsWith('page:')) references.delete(key);
  }

  async function loadDiscussions(swarm = host.selectedSwarm, cursor = null) {
    if (!swarm) return;
    const selection = host.selectionRequest;
    const request = ++discussionsRequest;
    const result = await host.call('board.list', {
      swarm_id: swarm.id,
      limit: 100,
      ...(cursor ? { cursor } : {}),
    });
    if (
      host.disposed ||
      selection !== host.selectionRequest ||
      request !== discussionsRequest ||
      host.selectedSwarm?.id !== swarm.id
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
    posts = [];
    boardCursor = null;
    boardAtNewest = true;
    loaded = false;
  }

  const shows = (swarm, discussionId) =>
    boardShown?.swarmId === swarm.id &&
    boardShown.discussionId === discussionId;

  const boardCurrent = (version, swarm, discussionId) =>
    !host.disposed &&
    version === boardVersion &&
    host.selectedSwarm?.id === swarm.id &&
    selectedDiscussion === discussionId;

  const readPosts = (swarm, discussionId, bounds = {}) =>
    host.call('board.read', {
      swarm_id: swarm.id,
      discussion_id: discussionId,
      limit: BOARD_PAGE,
      ...bounds,
    });

  // Loads the discussion's newest page, or with `cursor` the earlier page
  // below the posts the Board shows.
  async function loadBoard(
    swarm = host.selectedSwarm,
    discussionId = selectedDiscussion,
    cursor = null,
  ) {
    if (!swarm) return;
    const version = cursor ? boardVersion : ++boardVersion;
    const load = readPosts(swarm, discussionId, cursor ? { cursor } : {});
    if (!cursor) boardLoading = load;
    let result;
    try {
      result = await load;
    } finally {
      if (boardLoading === load) boardLoading = null;
    }
    if (!boardCurrent(version, swarm, discussionId)) return;
    const entries = [...page(result)].reverse();
    if (cursor) {
      if (!shows(swarm, discussionId)) return;
      const oldest = posts.at(-1)?.sequence ?? Infinity;
      posts = [...posts, ...entries.filter((post) => post.sequence < oldest)];
    } else {
      posts = entries;
      boardShown = { swarmId: swarm.id, discussionId };
      boardAtNewest = true;
      loaded = true;
    }
    boardCursor = result.cursor ?? null;
  }

  // Shows the post numbered `number` of a discussion with up to a page of
  // earlier posts below it and up to a page of newer ones above, read
  // together. Returns whether the Board shows them.
  async function loadAround(swarm, discussionId, number) {
    const version = ++boardVersion;
    const load = Promise.all([
      readPosts(swarm, discussionId, { before: number + 1 }),
      readPosts(swarm, discussionId, { after: number }),
    ]);
    boardLoading = load;
    let earlier, newer;
    try {
      [earlier, newer] = await load;
    } finally {
      if (boardLoading === load) boardLoading = null;
    }
    if (!boardCurrent(version, swarm, discussionId)) return false;
    // The two reads meet at `number`; a post in both would be listed twice.
    posts = page(newer)
      .filter((post) => post.sequence > number)
      .reverse()
      .concat(
        page(earlier)
          .filter((post) => post.sequence <= number)
          .reverse(),
      );
    boardShown = { swarmId: swarm.id, discussionId };
    boardCursor = earlier.cursor ?? null;
    boardAtNewest = !newer.has_more;
    loaded = true;
    return true;
  }

  // Puts newer posts of `result`, oldest first, on top of the Board.
  function putNewer(result) {
    const newest = posts[0]?.sequence ?? 0;
    posts = [
      ...page(result)
        .filter((post) => post.sequence > newest)
        .reverse(),
      ...posts,
    ];
  }

  // Puts the selected discussion's posts newer than the Board's newest on top
  // while the Board shows the discussion's newest posts. A Board that does
  // not show the discussion yet loads its newest page.
  async function loadNewPosts(swarm) {
    const discussionId = selectedDiscussion;
    // A load in flight may have read before the newest posts.
    await boardLoading?.catch(() => {});
    await boardNewerLoading?.catch(() => {});
    if (!shows(swarm, discussionId)) return loadBoard(swarm, discussionId);
    // The newer posts the Board does not show come with "Load newer
    // messages" or "Jump to newest".
    if (!boardAtNewest) return;
    const version = boardVersion;
    for (;;) {
      const result = await readPosts(swarm, discussionId, {
        after: posts[0]?.sequence ?? 0,
      });
      if (!boardCurrent(version, swarm, discussionId)) return;
      putNewer(result);
      if (!result.has_more || !page(result).length) return;
    }
  }

  // "Load newer messages", a user step: puts the next page of newer posts on
  // top of a Board that does not show the discussion's newest posts.
  async function loadNewerPosts() {
    const swarm = host.selectedSwarm;
    const discussionId = selectedDiscussion;
    if (!swarm || boardAtNewest || !shows(swarm, discussionId)) return;
    host.error = '';
    const version = boardVersion;
    const load = readPosts(swarm, discussionId, {
      after: posts[0]?.sequence ?? 0,
    });
    boardNewerLoading = load;
    let result;
    try {
      result = await load;
    } catch (cause) {
      if (boardCurrent(version, swarm, discussionId))
        host.error = failureText(cause);
      return;
    } finally {
      if (boardNewerLoading === load) boardNewerLoading = null;
    }
    if (!boardCurrent(version, swarm, discussionId)) return;
    putNewer(result);
    boardAtNewest = !result.has_more;
  }

  // "Jump to newest", a user step: replaces the shown posts with the
  // discussion's newest page and shows its newest post.
  async function jumpToNewest() {
    const swarm = host.selectedSwarm;
    if (!swarm) return;
    host.error = '';
    const version = boardVersion + 1;
    try {
      await loadBoard(swarm, selectedDiscussion);
    } catch (cause) {
      if (!host.disposed && version === boardVersion)
        host.error = failureText(cause);
      return;
    }
    if (host.disposed || version !== boardVersion) return;
    await tick();
    revealElement(document.querySelector('.board > li'));
  }

  // Shows the discussion `id` from its newest posts. A user step: its failure
  // shows as the page error unless a newer read replaced it.
  async function chooseDiscussion(id) {
    const swarm = host.selectedSwarm;
    selectedDiscussion = id;
    clearBoard();
    if (!swarm) return;
    host.error = '';
    const version = boardVersion + 1;
    try {
      await loadBoard(swarm, id);
    } catch (cause) {
      if (boardCurrent(version, swarm, id)) host.error = failureText(cause);
    }
  }

  function openDiscussion(announcement) {
    if (!discussions.some((item) => item.id === announcement.discussion_id))
      discussions = [
        ...discussions,
        { id: announcement.discussion_id, title: announcement.title },
      ];
    return chooseDiscussion(announcement.discussion_id);
  }

  // "Load more discussions" in the discussion picker, a user step.
  async function loadMoreDiscussions() {
    const swarm = host.selectedSwarm;
    if (!swarm || !discussionCursor) return;
    host.error = '';
    const selection = host.selectionRequest;
    const request = discussionsRequest + 1;
    try {
      await loadDiscussions(swarm, discussionCursor);
    } catch (cause) {
      if (
        !host.disposed &&
        selection === host.selectionRequest &&
        request === discussionsRequest &&
        host.selectedSwarm?.id === swarm.id
      )
        host.error = failureText(cause);
    }
  }

  // "Load earlier posts" below the posts the Board shows, a user step.
  async function loadEarlierPosts() {
    const swarm = host.selectedSwarm;
    const discussionId = selectedDiscussion;
    if (!swarm || !boardCursor) return;
    host.error = '';
    const version = boardVersion;
    try {
      await loadBoard(swarm, discussionId, boardCursor);
    } catch (cause) {
      if (boardCurrent(version, swarm, discussionId))
        host.error = failureText(cause);
    }
  }

  // Opens the composer for a new post, or with `post` for a reply to it.
  function compose(post = null) {
    replyTarget = post
      ? {
          id: post.id,
          sequence: post.sequence,
          preview: {
            title:
              post.author?.kind === 'user'
                ? t('swarm.you')
                : (post.author_name ?? post.author?.name ?? ''),
            text: opening(post.text),
          },
        }
      : null;
    composeOpen = true;
  }

  async function post() {
    if (posting || !host.selectedSwarm || !postText.trim()) return;
    // In roster order, so a retry sends the same request.
    const addressed = (host.selectedSwarm.participants ?? [])
      .filter((participant) => recipients[participant.id])
      .map((participant) => participant.id);
    const payload = {
      swarm_id: host.selectedSwarm.id,
      discussion_id: selectedDiscussion,
      text: postText,
      ...(replyTarget ? { reply_to: replyTarget.id } : {}),
      ...(addressed.length ? { recipients: addressed } : {}),
    };
    const fingerprint = JSON.stringify(payload);
    if (postMutation?.fingerprint !== fingerprint)
      postMutation = { fingerprint, id: requestId() };
    posting = true;
    host.error = '';
    try {
      const result = await host.call('board.post', {
        ...payload,
        request_id: postMutation.id,
      });
      postMutation = null;
      postText = '';
      recipients = {};
      replyTarget = null;
      composeOpen = false;
      await host.refresh();
      host.showResumeFailures(result);
    } catch (cause) {
      host.error = failureText(cause);
    } finally {
      posting = false;
    }
  }

  function contentLinks(node, { references: linked = true } = {}) {
    node.addEventListener('click', openContentLink);
    const numbers = linked
      ? referenceLinks(node, {
          target: referenceTarget,
          participants: () => host.selectedSwarm?.participants ?? [],
          describe: describeReference,
          loading: (kind, number) => ({
            title: kind === 'post' ? `#${number}` : `w${number}`,
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
    const swarm = host.selectedSwarm;
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

  // A participant's details card, read when it shows; a post's author and
  // opening, or a page's title and opening, read once.
  function describeReference(kind, number) {
    if (kind === 'participant')
      return () => {
        const participant = host.selectedSwarm?.participants?.find(
          (item) => item.id === number,
        );
        return participant ? participantCard(participant) : '';
      };
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
      posts.find((item) => item.sequence === number) ??
      page(
        await host.call('board.read', {
          swarm_id: host.selectedSwarm.id,
          message_id: `#${number}`,
        }),
      )[0];
    return {
      title:
        post.author?.kind === 'user'
          ? t('swarm.you')
          : (post.author?.name ?? t('swarm.participant')),
      text: opening(post.text),
    };
  }

  async function describePage(number) {
    const result = await host.call('wiki', {
      swarm_id: host.selectedSwarm.id,
      action: 'read',
      page_id: `w${number}`,
      limit: 300,
    });
    return {
      title: result.deleted
        ? t('swarm.references.deletedPage', { title: result.title })
        : result.title,
      text: opening(result.content),
    };
  }

  // Lists the discussion `discussionId` in the selector, reading its pages
  // until they name it; with `reread`, from a fresh first page.
  async function listDiscussion(swarm, discussionId, current, reread) {
    if (reread || !discussions.length) await loadDiscussions(swarm);
    while (
      !discussions.some((item) => item.id === discussionId) &&
      discussionCursor &&
      current()
    ) {
      const cursor = discussionCursor;
      await loadDiscussions(swarm, cursor);
      if (discussionCursor === cursor) break;
    }
    if (current() && !discussions.some((item) => item.id === discussionId))
      discussions = [...discussions, { id: discussionId }];
  }

  // Shows a cited post on the Board, focused in the view. A post the Board
  // does not show yet takes two round trips: its discussion, then the post
  // with the posts around it. A Board that was off screen reads what it shows
  // anew, as a step to the Board tab does.
  async function revealPost(number) {
    const swarm = host.selectedSwarm;
    if (!swarm) return;
    const request = ++revealRequest;
    const current = () =>
      !host.disposed &&
      request === revealRequest &&
      host.selectedSwarm?.id === swarm.id;
    const fresh = panels > 0;
    const held = () =>
      shows(swarm, selectedDiscussion) &&
      posts.some((item) => item.sequence === number);
    // A step to the Board, which loads only what the post needs.
    host.stepToBoard();
    if (number === swarm.goal_post_sequence) {
      if (!fresh) await reload(swarm, null);
      await tick();
      const goal = document.querySelector('.swarm-goal-post');
      if (goal && current()) {
        goal.open = true;
        revealElement(goal, current);
      }
      return;
    }
    if (!fresh || !held()) {
      // A post the Board holds is in the selected discussion; another one
      // names its discussion when read by number.
      let discussionId = selectedDiscussion;
      if (!held()) {
        const [post] = page(
          await host.call('board.read', {
            swarm_id: swarm.id,
            message_id: `#${number}`,
          }),
        );
        if (!post || !current()) return;
        discussionId = post.discussion_id;
      }
      if (!fresh || !held()) {
        if (selectedDiscussion !== discussionId) {
          selectedDiscussion = discussionId;
          clearBoard();
        }
        const [around] = await Promise.all([
          loadAround(swarm, discussionId, number),
          listDiscussion(swarm, discussionId, current, !fresh),
        ]);
        if (!around) return;
      }
    }
    if (!current()) return;
    // A revealed post shows whole.
    const revealed = posts.find((item) => item.sequence === number);
    if (revealed) opened[revealed.id] = true;
    await tick();
    revealElement(
      document.querySelector(`.board > li[data-post-number="${number}"]`),
      current,
    );
  }

  // Folds a post body that runs past COLLAPSIBLE_LINES lines while it is laid
  // out. Posts away from the view are not laid out (`content-visibility`), so
  // a body is measured only once it comes near the view, before it is
  // painted.
  function postBody(node, id) {
    bodyPosts.set(node, id);
    if (typeof ResizeObserver !== 'function') return {};
    bodies ??= new ResizeObserver((entries) => {
      for (const entry of entries)
        if (entry.contentRect.height > 0) measureBody(entry.target);
    });
    bodies.observe(node);
    return {
      update(next) {
        bodyPosts.set(node, next);
      },
      destroy() {
        bodies?.unobserve(node);
      },
    };
  }

  function measureBody(node) {
    const id = bodyPosts.get(node);
    if (id === undefined) return;
    const lineHeight =
      parseFloat(
        getComputedStyle(node.querySelector('.msg-markdown') ?? node)
          .lineHeight,
      ) || 20;
    // A folded body keeps its whole text's scroll height.
    const folds = node.scrollHeight > lineHeight * COLLAPSIBLE_LINES;
    if (folds) folding[id] = true;
    else if (folding[id]) delete folding[id];
  }

  // Opens a folded post, or folds it again. A post folded again keeps its
  // Show more in view.
  async function toggleExpanded(id, control) {
    if (!opened[id]) {
      opened[id] = true;
      return;
    }
    delete opened[id];
    await tick();
    if (control?.isConnected && control.getBoundingClientRect().top < 0)
      control.scrollIntoView?.({ block: 'nearest' });
  }

  // Scrolls `element` into view (see `place`), then focuses and highlights
  // it. A short way scrolls smoothly; a long one jumps, since smooth
  // scrolling would render every post on the way.
  function revealElement(element, current = () => !host.disposed) {
    if (!element) return;
    const reduced = window.matchMedia?.(
      '(prefers-reduced-motion: reduce)',
    )?.matches;
    const view = viewOf(element);
    const box = element.getBoundingClientRect();
    const near =
      Math.abs(box.top + box.height / 2 - (view.top + view.bottom) / 2) <
      view.bottom - view.top;
    element.scrollIntoView?.({
      block: place(element, box, view),
      behavior: near && !reduced ? 'smooth' : 'auto',
    });
    if (!near) recenter(element, current, view);
    element.tabIndex = -1;
    element.focus({ preventScroll: true });
    element.dataset.revealed = '';
    setTimeout(() => delete element.dataset.revealed, 2000);
  }

  // The part of the window that the scroller holding `element` shows, and
  // the scroller's top padding, below which sticky bars stick.
  function viewOf(element) {
    let scroller = element.parentElement;
    while (
      scroller &&
      scroller !== document.body &&
      !/^(auto|scroll)$/.test(getComputedStyle(scroller).overflowY)
    )
      scroller = scroller.parentElement;
    const own = scroller && scroller !== document.body;
    const box = own
      ? scroller.getBoundingClientRect()
      : { top: 0, bottom: window.innerHeight };
    return {
      top: Math.max(box.top, 0),
      bottom: Math.min(box.bottom, window.innerHeight),
      inset: own ? parseFloat(getComputedStyle(scroller).paddingTop) || 0 : 0,
    };
  }

  // Where a reveal puts `element`, whose box is `box`: in the middle of the
  // room below what sticks at the top of the view, or with its top just
  // below it when it is taller than that room. Returns the `scrollIntoView`
  // block.
  function place(element, box, view) {
    element.style.scrollMarginTop = '';
    // What the Run shell makes stick above the panels: the stylesheet's
    // scroll margin, an offset below the scroller's top padding, as sticky
    // `top` values are.
    const shell = parseFloat(getComputedStyle(element).scrollMarginTop) || 0;
    const bar = element.matches('.board > li')
      ? document.querySelector('.board-newer')
      : null;
    // How far into the view what sticks reaches: the newer-posts bar above
    // posts, otherwise the shell's parts or the scroller's top padding.
    const stuck = bar
      ? view.inset +
        (parseFloat(getComputedStyle(bar).top) || 0) +
        bar.getBoundingClientRect().height
      : view.inset + shell;
    const tall = box.height > view.bottom - view.top - stuck;
    const margin = stuck + (tall && (bar || shell) ? 8 : 0);
    element.style.scrollMarginTop = margin ? `${margin}px` : '';
    return tall ? 'start' : 'center';
  }

  // Posts away from the view render only near it (`content-visibility`), so
  // a jump places `element` by estimated heights of the posts on the way,
  // its own included. Keeps it in place while posts coming into view replace
  // their estimates: until it stays put for two frames, for at most
  // RECENTER_FRAMES frames, and never once the reader scrolls, touches, or
  // presses a key or a pointer button.
  function recenter(element, current, view) {
    let frames = RECENTER_FRAMES;
    let still = 0;
    let box = element.getBoundingClientRect();
    let taken = false;
    const take = () => (taken = true);
    for (const type of READER_INPUT)
      window.addEventListener(type, take, { capture: true, passive: true });
    const finish = () => {
      for (const type of READER_INPUT)
        window.removeEventListener(type, take, { capture: true });
    };
    const step = () => {
      if (taken || !current() || !element.isConnected || frames-- <= 0)
        return finish();
      const next = element.getBoundingClientRect();
      if (next.top === box.top && next.height === box.height) {
        if (++still === 2) return finish();
      } else {
        still = 0;
        element.scrollIntoView?.({
          block: place(element, next, view),
          behavior: 'auto',
        });
        box = element.getBoundingClientRect();
      }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function openContentLink(event) {
    const link = event.target.closest('a[href]');
    if (!link) return;
    event.preventDefault();
    const href = link.getAttribute('href') ?? '';
    const post = /^#post\/(\d+)$/.exec(href);
    if (post) {
      void Promise.resolve(
        host.navigate(() => revealPost(Number(post[1]))),
      ).catch((cause) => {
        if (!host.disposed) host.error = failureText(cause);
      });
      return;
    }
    const wiki = /^#wiki\/([^/]+)$/.exec(href);
    if (wiki) {
      void host.navigate(() => host.openWikiPage(wiki[1]));
      return;
    }
    // A mention shows the participant's Activity, as its chip does.
    const mention = /^#participant\/(.+)$/.exec(href);
    if (mention) {
      const participant = host.selectedSwarm?.participants?.find(
        (item) => encodeURIComponent(item.id) === mention[1],
      );
      if (participant)
        void Promise.resolve(
          host.navigate(() => host.inspectParticipant(participant)),
        ).catch((cause) => {
          if (!host.disposed) host.error = failureText(cause);
        });
      return;
    }
    // A discussion announcement opens the discussion it announces.
    const discussion = /^#discussion\/(.+)$/.exec(href);
    if (discussion) {
      const announced = posts.find(
        (item) =>
          encodeURIComponent(
            item.discussion_announcement?.discussion_id ?? '',
          ) === discussion[1],
      )?.discussion_announcement;
      if (announced) void openDiscussion(announced);
      return;
    }
    const url = link.href;
    if (!url) return;
    const isMedia =
      Boolean(link.querySelector('img, video, audio')) ||
      /\.(avif|gif|jpe?g|mp3|mp4|ogg|png|svg|webm)(?:$|[?#])/i.test(url);
    void (
      isMedia ? host.client.openMedia(url) : host.client.openLink(url)
    ).catch((cause) => (host.error = failureText(cause)));
  }

  return {
    get posts() {
      return posts;
    },
    get boardCursor() {
      return boardCursor;
    },
    get boardAtNewest() {
      return boardAtNewest;
    },
    get loaded() {
      return loaded;
    },
    get discussionOptions() {
      return discussionOptions;
    },
    get discussionTitle() {
      return discussionTitle;
    },
    get selectedDiscussion() {
      return selectedDiscussion;
    },
    get discussionCursor() {
      return discussionCursor;
    },
    get discussionParticipants() {
      return discussionParticipants;
    },
    // Whether post `id` folds, and whether the reader opened it.
    folds: (id) => folding[id] === true,
    opened: (id) => opened[id] === true,
    get composeOpen() {
      return composeOpen;
    },
    set composeOpen(value) {
      composeOpen = value;
    },
    get postText() {
      return postText;
    },
    set postText(value) {
      postText = value;
    },
    // Whether the post addresses participant `id`; `address` changes it.
    addresses: (id) => recipients[id] === true,
    address(id, chosen) {
      if (chosen) recipients[id] = true;
      else delete recipients[id];
    },
    get replyTarget() {
      return replyTarget;
    },
    clearReply() {
      replyTarget = null;
    },
    get posting() {
      return posting;
    },
    reset,
    reload,
    forgetPageReferences,
    loadMoreDiscussions,
    loadEarlierPosts,
    loadNewerPosts,
    jumpToNewest,
    chooseDiscussion,
    openDiscussion,
    compose,
    post,
    contentLinks,
    // Marks the Board panel on screen while it is mounted.
    panel() {
      panels += 1;
      return {
        destroy() {
          panels -= 1;
        },
      };
    },
    postBody,
    toggleExpanded,
    expand(id) {
      opened[id] = true;
    },
  };
}
