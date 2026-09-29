/**
 * Settings search over the mounted Settings DOM.
 *
 * SettingsView keeps every page and section mounted, hidden ones included, so
 * the rendered configuration page grammar (`styles/settings/sections.css`)
 * is the index and cannot drift from what the pages show. An index holds:
 *
 * - one entry per page (label and description) and one per titled section;
 * - one entry per label element inside a section: `.s-row-label`,
 *   `.s-subhead__title`, FormField labels and declared `[data-search-label]`
 *   elements. A label in an entity head (`.s-entity__head`) stands for the
 *   whole entity, so the text of its collapsed details counts; any other
 *   label stands for its row (the parent of its `.s-row-info`), its FormField
 *   or its parent element.
 *
 * `[data-search-label]` declares a setting that a panel renders only on
 * demand (the fields of a Channel's edit form): a hidden element carrying the
 * label and, optionally, extra words in `data-search-terms`. It is its own
 * entry and yields to a rendered entry with the same label in its section.
 *
 * Revealing an entry (`expandSettingsEntry`, `settingsEntryAnchor`,
 * `focusSettingsControl`) opens what hides it through the page's own
 * disclosure controls and picks the element to scroll to and focus; the
 * caller owns navigation, scrolling and timing.
 */

const LABEL_SELECTOR =
  '.s-row-label, .s-subhead__title, .form-field__label, [data-search-label]';
const DESCRIPTION_SELECTOR = '.s-row-desc, .s-subhead__desc, .form-field__help';
const HELP_SELECTOR = '[data-help-text]';
// Decorations that are not part of a label's visible name.
const LABEL_DECORATION = '[data-help-text], [aria-hidden="true"]';
const CONTROL_SELECTOR =
  'button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])';
const SKIPPED_CONTROL =
  '[data-help-text], :disabled, [aria-disabled="true"], [type="hidden"]';
const KIND_RANK = { page: 0, section: 1, row: 2 };

export const SETTINGS_SEARCH_LIMIT = 40;

// Case, diacritics, spacing, punctuation and symbols do not matter.
export function normalizeSearchText(value) {
  return String(value ?? '')
    .normalize('NFKD')
    .toLocaleLowerCase()
    .replace(/[\p{M}\s\p{P}\p{S}]/gu, '');
}

export function searchTerms(query) {
  return String(query ?? '')
    .trim()
    .split(/\s+/u)
    .map(normalizeSearchText)
    .filter(Boolean);
}

function collapseWhitespace(value) {
  return String(value ?? '')
    .replace(/\s+/gu, ' ')
    .trim();
}

// A label's normalized text plus the offsets where its words start, so a term
// can be recognized as the beginning of a word.
function labelField(label) {
  let text = '';
  const starts = [];
  for (const word of label.split(/[\s\p{P}\p{S}]+/u)) {
    const part = normalizeSearchText(word);
    if (!part) continue;
    starts.push(text.length);
    text += part;
  }
  return { text, starts };
}

function ownText(node) {
  let text = '';
  for (const child of node.childNodes) {
    if (child.nodeType === 3) text += child.nodeValue;
    else if (child.nodeType === 1 && !child.matches(LABEL_DECORATION))
      text += ownText(child);
  }
  return text;
}

function helpTexts(element) {
  return Array.from(element.querySelectorAll(HELP_SELECTOR), (hint) =>
    normalizeSearchText(hint.dataset.helpText),
  ).filter(Boolean);
}

function descriptionTexts(element) {
  return Array.from(element.querySelectorAll(DESCRIPTION_SELECTOR), (item) =>
    normalizeSearchText(item.textContent),
  ).filter(Boolean);
}

function ownFields(label, { description = [], help = [], content = '' } = {}) {
  return {
    label: labelField(label),
    description,
    help,
    content,
  };
}

// The entry a label element stands for, or null for an empty label.
function rowEntry(labelElement) {
  if (labelElement.matches('[data-search-label]')) {
    const label = collapseWhitespace(labelElement.dataset.searchLabel);
    if (!label) return null;
    const terms = normalizeSearchText(labelElement.dataset.searchTerms);
    return {
      declared: true,
      label,
      element: labelElement,
      head: null,
      own: ownFields(label, { help: terms ? [terms] : [] }),
    };
  }
  const label = collapseWhitespace(ownText(labelElement));
  if (!label) return null;
  const head = labelElement.closest('.s-entity__head');
  const entity = head?.closest('.s-entity') ?? null;
  const element =
    entity ??
    labelElement.closest('.s-row-info')?.parentElement ??
    labelElement.closest('.form-field') ??
    labelElement.parentElement;
  if (!element) return null;
  return {
    declared: false,
    label,
    element,
    // An entity matched only through its collapsed details opens them.
    head: entity
      ? [normalizeSearchText(head.textContent), ...helpTexts(head)]
      : null,
    own: ownFields(label, {
      description: descriptionTexts(head ?? element),
      help: helpTexts(element),
      content: normalizeSearchText(element.textContent),
    }),
  };
}

function sectionRows(container, page, section, context) {
  const rows = [];
  const elements = new Set();
  for (const labelElement of container.querySelectorAll(LABEL_SELECTOR)) {
    const row = rowEntry(labelElement);
    if (!row || elements.has(row.element)) continue;
    elements.add(row.element);
    rows.push(row);
  }
  // A declared setting yields to the rendered row it stands for.
  const rendered = new Set(
    rows.filter((row) => !row.declared).map((row) => row.own.label.text),
  );
  return rows
    .filter((row) => !row.declared || !rendered.has(row.own.label.text))
    .map((row, index) => ({
      kind: 'row',
      key: `${section.id}:${index}:${row.label}`,
      pageId: page.id,
      sectionId: section.id,
      label: row.label,
      element: row.element,
      head: row.head,
      own: row.own,
      context,
    }));
}

/**
 * Builds the index of the Settings DOM under `root`. `pages` lists the
 * translated page and section labels in display order:
 * `[{id, label, description, sections: [{id, label}]}]`.
 */
export function indexSettings(root, pages) {
  const index = [];
  for (const page of pages) {
    const pageContext = normalizeSearchText(page.label);
    index.push({
      kind: 'page',
      key: `page:${page.id}`,
      pageId: page.id,
      sectionId: '',
      label: page.label,
      element: null,
      head: null,
      own: ownFields(page.label, {
        description: [normalizeSearchText(page.description)],
      }),
      context: [],
    });
    // A single section shares its page's heading.
    const titled = page.sections.length > 1;
    for (const section of page.sections) {
      if (titled)
        index.push({
          kind: 'section',
          key: `section:${section.id}`,
          pageId: page.id,
          sectionId: section.id,
          label: section.label,
          element: null,
          head: null,
          own: ownFields(section.label),
          context: [pageContext],
        });
      const container = root?.querySelector(
        `[data-settings-section="${section.id}"]`,
      );
      if (container)
        index.push(
          ...sectionRows(container, page, section, [
            pageContext,
            normalizeSearchText(section.label),
          ]),
        );
    }
  }
  index.forEach((entry, order) => {
    entry.order = order;
  });
  return index;
}

// Where a term matches an entry's own fields: its label (a word starting with
// the term counts more), its description, then its help and content.
function termScore(own, term) {
  if (own.label.text.includes(term))
    return own.label.starts.some((start) =>
      own.label.text.startsWith(term, start),
    )
      ? 4
      : 3;
  if (own.description.some((text) => text.includes(term))) return 2;
  if (
    own.help.some((text) => text.includes(term)) ||
    own.content.includes(term)
  )
    return 1;
  return 0;
}

function matchEntry(entry, terms) {
  let score = 0;
  let openDetails = false;
  for (const term of terms) {
    const points = termScore(entry.own, term);
    const inContext = entry.context.some((text) => text.includes(term));
    if (!points && !inContext) return null;
    score += points;
    if (
      points &&
      entry.head &&
      !inContext &&
      !entry.head.some((text) => text.includes(term))
    )
      openDetails = true;
  }
  // Page and section labels alone do not list every row they hold.
  return score > 0 ? { entry, score, openDetails } : null;
}

/**
 * The entries matching every term of `query`, best first. Each term must
 * occur in the entry's own text or in the labels of the page and section
 * holding it, and at least one term in the entry's own text. An entry that
 * contains another matching entry (an entity whose inner row matches) is
 * left out. Results: `{key, kind, pageId, sectionId, label, element,
 * openDetails}`, `kind` being `page`, `section` or `row`.
 */
export function matchSettings(
  index,
  query,
  { limit = SETTINGS_SEARCH_LIMIT } = {},
) {
  const terms = searchTerms(query);
  if (terms.length === 0) return [];
  const matches = [];
  for (const entry of index) {
    const match = matchEntry(entry, terms);
    if (match) matches.push(match);
  }
  const rows = matches.filter((match) => match.entry.kind === 'row');
  return matches
    .filter(
      (match) =>
        match.entry.kind !== 'row' ||
        !rows.some(
          (other) =>
            other !== match &&
            match.entry.element.contains(other.entry.element),
        ),
    )
    .sort(
      (left, right) =>
        right.score - left.score ||
        KIND_RANK[left.entry.kind] - KIND_RANK[right.entry.kind] ||
        left.entry.order - right.entry.order,
    )
    .slice(0, limit)
    .map(({ entry, openDetails }) => ({
      key: entry.key,
      kind: entry.kind,
      pageId: entry.pageId,
      sectionId: entry.sectionId,
      label: entry.label,
      element: entry.element,
      openDetails,
    }));
}

function pressDisclosure(container, id) {
  const control = Array.from(
    container.querySelectorAll('[aria-controls][aria-expanded="false"]'),
  ).find((candidate) =>
    candidate.getAttribute('aria-controls').split(/\s+/u).includes(id),
  );
  if (!control || control.disabled) return false;
  control.click();
  return true;
}

/**
 * Opens what hides a row result inside `container` (its section): closed
 * `<details>` ancestors, hidden ancestors whose `[aria-controls]` control
 * reports `aria-expanded="false"` (clicked, so the panel's own toggle runs),
 * and an entity's details when the match lies there. Returns whether
 * anything was opened; the caller lets the page update before anchoring.
 */
export function expandSettingsEntry({ element, openDetails }, container) {
  if (!element?.isConnected || !container?.contains(element)) return false;
  const path = [];
  for (
    let node = element;
    node && node !== container;
    node = node.parentElement
  )
    path.unshift(node);
  let expanded = false;
  for (const node of path) {
    if (node !== element && node.localName === 'details' && !node.open) {
      node.open = true;
      expanded = true;
    }
    if (node.hidden && node.id && pressDisclosure(container, node.id))
      expanded = true;
  }
  if (openDetails) {
    const head = element.querySelector('.s-entity__head');
    const control = head?.querySelector(
      '[aria-controls][aria-expanded="false"]',
    );
    if (control && !control.disabled) {
      control.click();
      expanded = true;
    }
  }
  return expanded;
}

function previousVisibleRow(row) {
  for (
    let node = row.previousElementSibling;
    node;
    node = node.previousElementSibling
  )
    if (node.matches('.s-row') && !node.hidden) return node;
  return null;
}

/**
 * The element to scroll to for a result inside `body` (its section body): the
 * entry itself when visible; for a row hidden while the setting it depends on
 * is off, the visible row before it (the switch that reveals it); otherwise
 * the nearest visible ancestor. Null when only the section itself remains.
 */
export function settingsEntryAnchor(element, body) {
  if (!element?.isConnected || !body?.contains(element)) return null;
  let hidden = null;
  for (let node = element; node && node !== body; node = node.parentElement)
    if (node.hidden) hidden = node;
  const anchor = !hidden
    ? element
    : (hidden.matches('.s-row') && previousVisibleRow(hidden)) ||
      hidden.parentElement;
  return anchor && anchor !== body && body.contains(anchor) ? anchor : null;
}

/**
 * Focuses the first enabled, visible control inside `anchor` (help "?"
 * buttons excluded) without scrolling. Returns whether a control took focus.
 */
export function focusSettingsControl(anchor) {
  const controls = anchor.matches(CONTROL_SELECTOR)
    ? [anchor, ...anchor.querySelectorAll(CONTROL_SELECTOR)]
    : anchor.querySelectorAll(CONTROL_SELECTOR);
  for (const control of controls) {
    if (
      control.matches(SKIPPED_CONTROL) ||
      control.closest('[hidden], [inert]')
    )
      continue;
    control.focus({ preventScroll: true });
    if (control.ownerDocument.activeElement === control) return true;
  }
  return false;
}
