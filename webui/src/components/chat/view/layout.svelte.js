export function createChatViewLayout(context) {
  let composerFocusRequest = $state(0);

  // Live height of the floating composer stack over the timeline. The
  // surface exposes it as a CSS variable so the timeline reserves matching
  // bottom space and content scrolls out from behind the composer. Measured
  // with a guarded ResizeObserver instead of bind:clientHeight, which would
  // hard-require ResizeObserver even in layout-less environments.
  let footerOverlayHeight = $state(0);

  let footerStackElement = $state(null);

  // Real scrollbar width of the chat scroller; the composer stack ends
  // before this column so the scrollbar stays visible over its full height.
  let chatScrollbarWidth = $state(0);

  $effect(() => {
    const element = footerStackElement;
    if (!element || typeof ResizeObserver !== 'function') {
      return undefined;
    }
    const observer = new ResizeObserver(() => {
      footerOverlayHeight = element.clientHeight;
    });
    observer.observe(element);
    return () => observer.disconnect();
  });

  const MOBILE_CHAT_MEDIA_QUERY = '(max-width: 640px)';

  const requestComposerFocus = ({ includeMobile = false } = {}) => {
    if (!context.active || !context.interactive) return;
    const mobile =
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia(MOBILE_CHAT_MEDIA_QUERY).matches;
    if (!includeMobile && mobile) {
      return;
    }
    composerFocusRequest += 1;
  };

  // History normally arrives quickly enough that loading feedback would only
  // flash. Keep the transition calm, while still explaining a real delay.
  const HISTORY_LOADING_FEEDBACK_DELAY_MS = 300;

  let historyLoadingFeedbackVisible = $state(false);

  let historyLoadingFeedbackSessionKey = $derived(
    context.target.activeSessionState?.key ?? '',
  );

  $effect(() => {
    const sessionKey = historyLoadingFeedbackSessionKey;
    historyLoadingFeedbackVisible = false;
    if (!context.chatState.loadingHistory || !sessionKey) {
      return undefined;
    }
    const timeoutId = setTimeout(() => {
      if (
        context.chatState.loadingHistory &&
        historyLoadingFeedbackSessionKey === sessionKey
      ) {
        historyLoadingFeedbackVisible = true;
      }
    }, HISTORY_LOADING_FEEDBACK_DELAY_MS);
    return () => clearTimeout(timeoutId);
  });
  return {
    get composerFocusRequest() {
      return composerFocusRequest;
    },
    get footerOverlayHeight() {
      return footerOverlayHeight;
    },
    get footerStackElement() {
      return footerStackElement;
    },
    set footerStackElement(value) {
      footerStackElement = value;
    },
    get chatScrollbarWidth() {
      return chatScrollbarWidth;
    },
    set chatScrollbarWidth(value) {
      chatScrollbarWidth = value;
    },
    get requestComposerFocus() {
      return requestComposerFocus;
    },
    get historyLoadingFeedbackVisible() {
      return historyLoadingFeedbackVisible;
    },
  };
}
