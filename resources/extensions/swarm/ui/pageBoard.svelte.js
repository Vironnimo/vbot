import { SvelteMap } from 'svelte/reactivity';
import { tick } from 'svelte';
import { t } from '../../../../webui/src/lib/i18n.js';
import { page, requestId } from './pagePresentation.js';
import { swarmChanged } from './pageRefresh.js';
import { referenceLinks } from './referenceLinks.js';

// Posts per Board read: the newest page, an earlier or newer page, new posts,
// and each side of a revealed post. The Store also caps a page's text.
const BOARD_PAGE = 50;

// The most animation frames a revealed post is kept centered after a jump.
const RECENTER_FRAMES = 12;

// The selected Swarm's Board: the selected discussion's posts and their
// paging, the discussions, the post composer, and the links in rendered
// Markdown, which reveal cited posts on the Board. `host` is the page model's
// side: `call`, `client`, the shown `selectedSwarm` and its
// `selectionRequest`, `disposed`, the page `error`, and the page steps
// `navigate`, `stepToBoard`, `openWikiPage`, `refresh` and
// `showResumeFailures`.
export function createSwarmPageBoard(host) {
  let posts = $state([]);

  let boardCursor = $state(null);

  let discussions = $state([]);

  let selectedDiscussion = $state('');

  let discussionCursor = $state(null);

  let postText = $state('');

  let composeOpen = $state(false);

  let postRecipients = $state('');

  let replyTo = $state('');

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

  // Tooltip content for "#N" and "wN" links, by "post:N" and "page:N".
  const references = new SvelteMap();

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
    posts = [...page(newer)].reverse().concat([...page(earlier)].reverse());
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

  // Puts the next page of newer posts on top of a Board that does not show
  // the discussion's newest posts.
  async function loadNewerPosts() {
    const swarm = host.selectedSwarm;
    const discussionId = selectedDiscussion;
    if (!swarm || boardAtNewest || !shows(swarm, discussionId)) return;
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
        host.error = cause.message;
      return;
    } finally {
      if (boardNewerLoading === load) boardNewerLoading = null;
    }
    if (!boardCurrent(version, swarm, discussionId)) return;
    putNewer(result);
    boardAtNewest = !result.has_more;
  }

  // Replaces the shown posts with the discussion's newest page and shows its
  // newest post.
  async function jumpToNewest() {
    const swarm = host.selectedSwarm;
    if (!swarm) return;
    const version = boardVersion + 1;
    try {
      await loadBoard(swarm, selectedDiscussion);
    } catch (cause) {
      if (!host.disposed && version === boardVersion)
        host.error = cause.message;
      return;
    }
    if (host.disposed || version !== boardVersion) return;
    await tick();
    revealElement(document.querySelector('.board > li'));
  }

  async function chooseDiscussion(id) {
    selectedDiscussion = id;
    clearBoard();
    await loadBoard(host.selectedSwarm, id);
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
      host.error = cause.message;
    }
  }

  async function post() {
    if (posting || !host.selectedSwarm || !postText.trim()) return;
    const payload = {
      swarm_id: host.selectedSwarm.id,
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
    host.error = '';
    try {
      const result = await host.call('board.post', {
        ...payload,
        request_id: postMutation.id,
      });
      postMutation = null;
      postText = '';
      postRecipients = '';
      replyTo = '';
      composeOpen = false;
      await host.refresh();
      host.showResumeFailures(result);
    } catch (cause) {
      host.error = cause.message;
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
      posts.find((item) => item.sequence === number) ??
      page(
        await host.call('board.read', {
          swarm_id: host.selectedSwarm.id,
          message_id: `#${number}`,
        }),
      )[0];
    return {
      title: `#${number} · ${post.author?.name ?? t('swarm.participant')}`,
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
        ? t('swarm.references.deletedPage', {
            reference: `w${number}`,
            title: result.title,
          })
        : `w${number} · ${result.title}`,
      text: opening(result.content),
    };
  }

  // Lists the discussion `discussionId` in the selector, reading its pages
  // until they name it.
  async function listDiscussion(swarm, discussionId, current) {
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
    if (current() && !discussions.some((item) => item.id === discussionId))
      discussions = [...discussions, { id: discussionId }];
  }

  // Shows a cited post on the Board, focused in the middle of the view. A post
  // the Board does not show yet takes two round trips: its discussion, then
  // the post with the posts around it.
  async function revealPost(number) {
    const swarm = host.selectedSwarm;
    if (!swarm) return;
    const request = ++revealRequest;
    const current = () =>
      !host.disposed &&
      request === revealRequest &&
      host.selectedSwarm?.id === swarm.id;
    const shown = () =>
      shows(swarm, selectedDiscussion) &&
      posts.some((item) => item.sequence === number);
    // A step to the Board, which loads only what the post needs.
    host.stepToBoard();
    if (number === swarm.goal_post_sequence) {
      await tick();
      const goal = document.querySelector('.swarm-goal-post');
      if (goal && current()) {
        goal.open = true;
        revealElement(goal, current);
      }
      return;
    }
    if (!shown()) {
      const [post] = page(
        await host.call('board.read', {
          swarm_id: swarm.id,
          message_id: `#${number}`,
        }),
      );
      if (!post || !current()) return;
      const discussionId = post.discussion_id;
      if (!shown()) {
        if (selectedDiscussion !== discussionId) {
          selectedDiscussion = discussionId;
          clearBoard();
        }
        const [around] = await Promise.all([
          loadAround(swarm, discussionId, number),
          listDiscussion(swarm, discussionId, current),
        ]);
        if (!around) return;
      }
    }
    if (!current()) return;
    await tick();
    revealElement(
      document.querySelector(`.board > li[data-post-number="${number}"]`),
      current,
    );
  }

  // Scrolls `element` to the middle of the view, then focuses and highlights
  // it. A short way scrolls smoothly; a long one jumps, since smooth
  // scrolling would render every post on the way.
  function revealElement(element, current = () => !host.disposed) {
    if (!element) return;
    const reduced = window.matchMedia?.(
      '(prefers-reduced-motion: reduce)',
    )?.matches;
    const box = element.getBoundingClientRect();
    const near =
      Math.abs(box.top + box.height / 2 - window.innerHeight / 2) <
      window.innerHeight;
    element.scrollIntoView?.({
      block: 'center',
      behavior: near && !reduced ? 'smooth' : 'auto',
    });
    if (!near) recenter(element, current);
    element.tabIndex = -1;
    element.focus({ preventScroll: true });
    element.dataset.revealed = '';
    setTimeout(() => delete element.dataset.revealed, 2000);
  }

  // Posts away from the view render only near it (`content-visibility`), so
  // a jump places `element` by estimated heights of the posts on the way.
  // Keeps it centered while posts coming into view replace their estimates:
  // until it stays put for two frames, for at most RECENTER_FRAMES frames.
  function recenter(element, current) {
    let frames = RECENTER_FRAMES;
    let still = 0;
    let top = element.getBoundingClientRect().top;
    const step = () => {
      if (!current() || !element.isConnected || frames-- <= 0) return;
      if (element.getBoundingClientRect().top === top) {
        if (++still === 2) return;
      } else {
        still = 0;
        element.scrollIntoView?.({ block: 'center', behavior: 'auto' });
        top = element.getBoundingClientRect().top;
      }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function openContentLink(event) {
    const link = event.target.closest('a[href]');
    if (!link) return;
    event.preventDefault();
    const post = /^#post\/(\d+)$/.exec(link.getAttribute('href') ?? '');
    if (post) {
      void Promise.resolve(
        host.navigate(() => revealPost(Number(post[1]))),
      ).catch((cause) => {
        if (!host.disposed) host.error = cause.message;
      });
      return;
    }
    const wiki = /^#wiki\/([^/]+)$/.exec(link.getAttribute('href') ?? '');
    if (wiki) {
      void host.navigate(() => host.openWikiPage(wiki[1]));
      return;
    }
    const url = link.href;
    if (!url) return;
    const isMedia =
      Boolean(link.querySelector('img, video, audio')) ||
      /\.(avif|gif|jpe?g|mp3|mp4|ogg|png|svg|webm)(?:$|[?#])/i.test(url);
    void (
      isMedia ? host.client.openMedia(url) : host.client.openLink(url)
    ).catch((cause) => (host.error = cause.message));
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
    reset,
    reload,
    forgetPageReferences,
    loadDiscussions,
    loadBoard,
    loadNewerPosts,
    jumpToNewest,
    chooseDiscussion,
    openDiscussion,
    post,
    contentLinks,
  };
}
