import { mount, unmount } from 'svelte';
import { tooltip } from '$lib/tooltip.js';
import CopyButton from '../ui/CopyButton.svelte';

// Reconcile only Markdown-owned nodes. The source tree stays separate from
// the displayed tree: CopyButtons, file actions and tooltips decorate the
// latter, and must survive an unchanged block or growing text node.
// HTML comes exclusively from the Markdown renderer, which disables raw HTML.
function createMarkdownDom(target) {
  let records = [];
  let previousHtml = null;
  const sourceByNode = new WeakMap();

  function adoptSource(record, source) {
    record.source = source;
    sourceByNode.set(record.node, source);
    for (let index = 0; index < record.children.length; index += 1) {
      adoptSource(record.children[index], source.childNodes[index]);
    }
  }

  function reconcile(parent, previous, sources) {
    // A host can replace text with richer references (for example Swarm's
    // post links). On a changed subtree, rebuild children whose original
    // nodes no longer belong here instead of updating detached nodes. An
    // unchanged subtree never reaches this path and keeps its decorations.
    if (previous.some((record) => record.node.parentNode !== parent)) {
      parent.replaceChildren();
      previous = [];
    }
    const next = [];
    for (let index = 0; index < sources.length; index += 1) {
      const source = sources[index];
      const record = previous[index];
      if (
        record &&
        record.source.nodeType === source.nodeType &&
        record.source.nodeName === source.nodeName
      ) {
        if (!record.source.isEqualNode(source)) {
          if (source.nodeType === Node.ELEMENT_NODE) {
            for (const attribute of record.source.attributes) {
              if (!source.hasAttribute(attribute.name)) {
                record.node.removeAttribute(attribute.name);
              }
            }
            for (const attribute of source.attributes) {
              if (
                record.source.getAttribute(attribute.name) !== attribute.value
              ) {
                record.node.setAttribute(attribute.name, attribute.value);
              }
            }
            record.children = reconcile(record.node, record.children, [
              ...source.childNodes,
            ]);
          } else if (record.node.nodeValue !== source.nodeValue) {
            record.node.nodeValue = source.nodeValue;
          }
          record.source = source;
        } else {
          // Retain only the latest parsed source tree. Keeping an unchanged
          // node from an earlier parse also retains that entire old document
          // through its parent links, multiplying memory across stream chunks.
          adoptSource(record, source);
        }
        sourceByNode.set(record.node, source);
        next.push(record);
      } else {
        const node = source.cloneNode(false);
        const children = reconcile(node, [], [...source.childNodes]);
        if (record) record.node.replaceWith(node);
        else parent.append(node);
        sourceByNode.set(node, source);
        next.push({ source, node, children });
      }
    }
    for (const record of previous.slice(sources.length)) record.node.remove();
    return next;
  }

  return {
    sourceAttribute: (node, name) => sourceByNode.get(node)?.getAttribute(name),
    update(html) {
      if (html === previousHtml) return;
      previousHtml = html;
      const template = document.createElement('template');
      template.innerHTML = html;
      records = reconcile(target, records, [...template.content.childNodes]);
    },
  };
}

// Svelte action: rendering and decoration share one lifetime, so a streaming
// update only mounts/destroys controls whose own content actually changed.
export function renderMarkdownDom(target, options) {
  const dom = createMarkdownDom(target);
  const decorations = new Map();
  let caret;

  function update(options) {
    const { document: rendered, fileLinks, copyLabel, copiedLabel } = options;
    dom.update(rendered.html);
    const wanted = new Map();
    for (const slot of target.querySelectorAll('[data-markdown-code-index]')) {
      const block = rendered.codeBlocks[Number(slot.dataset.markdownCodeIndex)];
      if (!block?.copyable || !block.text) continue;
      wanted.set(slot, {
        signature: JSON.stringify(['code', block.text, copyLabel, copiedLabel]),
        mount() {
          const button = mount(CopyButton, {
            target: slot,
            props: {
              text: block.text,
              label: copyLabel,
              copiedLabel,
              class: 'msg-code__copy',
            },
          });
          return () => void unmount(button);
        },
      });
    }
    if (fileLinks) {
      for (const link of target.querySelectorAll(
        'a:not([data-file-external])',
      )) {
        const href = link.getAttribute('href') || '';
        let url;
        try {
          url = new URL(href, window.location.href);
        } catch {
          continue;
        }
        if (
          url.origin !== window.location.origin ||
          !/^\/api\/files\/[A-Za-z0-9_.-]+$/.test(url.pathname)
        )
          continue;
        const filename = link.textContent.trim();
        const path = dom.sourceAttribute(link, 'title') ?? '';
        const previewable = /\.html?$/i.test(filename);
        const actionHint = previewable
          ? options.previewHint
          : options.actionsHint;
        const externalLabel = options.externalLabel(filename);
        wanted.set(link, {
          signature: JSON.stringify([
            'file',
            href,
            filename,
            path,
            actionHint,
            externalLabel,
          ]),
          mount() {
            link.dataset.deliveredFile = href;
            link.dataset.fileName = filename;
            link.dataset.filePath = path;
            link.setAttribute('aria-haspopup', 'menu');
            link.removeAttribute('title');
            const pathHint = tooltip(link, {
              text: path,
              mono: true,
              selectable: true,
              rows: [{ value: actionHint, tone: 'muted' }],
            });
            let external;
            let externalHint;
            if (previewable) {
              link.dataset.previewFile = href;
              external = document.createElement('a');
              external.href = href;
              external.target = '_blank';
              external.rel = 'noopener noreferrer';
              external.dataset.fileExternal = '';
              external.dataset.previewFile = href;
              external.dataset.deliveredFile = href;
              external.dataset.fileName = filename;
              external.dataset.filePath = path;
              external.setAttribute('aria-label', externalLabel);
              external.textContent = '↗';
              link.after(external);
              externalHint = tooltip(external, externalLabel);
            }
            return () => {
              pathHint.destroy();
              externalHint?.destroy();
              external?.remove();
              delete link.dataset.deliveredFile;
              delete link.dataset.fileName;
              delete link.dataset.previewFile;
              delete link.dataset.filePath;
              link.removeAttribute('aria-haspopup');
              link.removeAttribute('aria-expanded');
              const title = dom.sourceAttribute(link, 'title');
              if (title !== null && title !== undefined)
                link.setAttribute('title', title);
            };
          },
        });
      }
    }
    for (const [node, decoration] of decorations) {
      if (wanted.get(node)?.signature === decoration.signature) continue;
      decoration.destroy();
      decorations.delete(node);
    }
    for (const [node, decoration] of wanted) {
      if (decorations.has(node)) continue;
      decorations.set(node, {
        signature: decoration.signature,
        destroy: decoration.mount(),
      });
    }
    if (options.caret) {
      if (!caret) {
        caret = document.createElement('span');
        caret.className = 'streaming-caret';
        caret.setAttribute('aria-hidden', 'true');
      }
      if (target.lastChild !== caret) target.append(caret);
    } else {
      caret?.remove();
    }
  }

  update(options);
  return {
    update,
    destroy() {
      for (const decoration of decorations.values()) decoration.destroy();
      decorations.clear();
    },
  };
}
