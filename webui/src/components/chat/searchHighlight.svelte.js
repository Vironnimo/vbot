// Each timeline owns its ranges within the document-wide highlight registry.
// Uppercase expands ligatures and sharp S; lowercasing also normalizes
// capital sharp S. Default Unicode folding keeps dotless i distinct.
function foldChar(char) {
  return char === 'ı' ? char : char.toLowerCase().toUpperCase().toLowerCase();
}

const firstRange = new WeakMap();
export function searchRange(element) {
  return firstRange.get(element);
}

export function searchHighlight(element, getTarget) {
  $effect(() => {
    const target = getTarget();
    if (!target) return;
    const registry = globalThis.CSS?.highlights;
    let ranges = [];
    function clear() {
      const highlight = registry?.get('chat-search');
      for (const range of ranges) highlight?.delete(range);
      ranges = [];
      firstRange.delete(element);
      if (highlight && !highlight.size) registry.delete('chat-search');
    }
    function paint() {
      clear();
      if (!registry) return;
      const highlight = registry.get('chat-search') ?? new Highlight();
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      let text = '';
      const nodes = [];
      let previousBlock;
      while (walker.nextNode()) {
        const node = walker.currentNode;
        const parent = node.parentElement;
        if (
          parent.closest(
            '.reasoning-block, button, [aria-hidden="true"], [hidden], script, style',
          )
        )
          continue;
        const block = parent.closest(
          'p, li, pre, td, th, h1, h2, h3, h4, h5, h6, div',
        );
        if (previousBlock && block !== previousBlock) text += '\n';
        previousBlock = block;
        nodes.push({ node, start: text.length });
        text += node.data;
      }
      let folded = '';
      const offsets = [];
      let sourceOffset = 0;
      for (const char of text) {
        const part = foldChar(char);
        for (let i = 0; i < part.length; i++) offsets.push(sourceOffset);
        folded += part;
        sourceOffset += char.length;
      }
      offsets.push(text.length);
      let first = Infinity;
      // eslint-disable-next-line svelte/prefer-svelte-reactivity -- deduplicate a single paint, never mutated
      for (const term of new Set(target.query.trim().split(/\s+/u))) {
        const needle = Array.from(term, foldChar).join('');
        let nodeIndex = 0;
        for (
          let at = folded.indexOf(needle);
          at !== -1;
          at = folded.indexOf(needle, at + 1)
        ) {
          const start = offsets[at];
          let endAt = at + needle.length;
          while (endAt < folded.length && offsets[endAt] === offsets[endAt - 1])
            endAt++;
          const end = offsets[endAt];
          while (
            nodeIndex + 1 < nodes.length &&
            nodes[nodeIndex + 1].start <= start
          )
            nodeIndex++;
          let last = nodeIndex;
          while (last + 1 < nodes.length && nodes[last + 1].start < end) last++;
          const range = document.createRange();
          range.setStart(nodes[nodeIndex].node, start - nodes[nodeIndex].start);
          range.setEnd(nodes[last].node, end - nodes[last].start);
          ranges.push(range);
          highlight.add(range);
          if (start < first) {
            firstRange.set(element, range);
            first = start;
          }
        }
      }
      if (ranges.length) registry.set('chat-search', highlight);
    }
    paint();
    const observer = new MutationObserver(paint);
    observer.observe(element, {
      childList: true,
      characterData: true,
      subtree: true,
    });
    return () => {
      observer.disconnect();
      clear();
    };
  });
}
