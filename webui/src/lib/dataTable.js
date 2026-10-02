// Pure rules of the shared table primitive (components/ui/DataTable.svelte):
// column defaults, the header-click sort transition, the default cell text,
// and the row projection (filter, then sort, then limit).
//
// A column is `{ id, label, align?, sortable?, sortValue?, defaultDirection?,
// format?, ... }`. Its value is `sortValue(row)` when given, else `row[id]`.
// Sorting compares numbers numerically and everything else as text with the
// locale's collator (numeric, case- and accent-insensitive). Missing values
// (null, undefined, NaN, invalid dates) always sort last, whatever the
// direction, and ties keep the incoming row order.

const MISSING_TEXT = '—';

const collators = new Map();
const numberFormats = new Map();

function collatorFor(locale) {
  let collator = collators.get(locale);
  if (!collator) {
    collator = new Intl.Collator(locale, {
      numeric: true,
      sensitivity: 'base',
    });
    collators.set(locale, collator);
  }
  return collator;
}

function numberFormatFor(locale) {
  let format = numberFormats.get(locale);
  if (!format) {
    format = new Intl.NumberFormat(locale);
    numberFormats.set(locale, format);
  }
  return format;
}

/** 'end' for right-aligned (numeric) columns, else 'start'. */
export function columnAlign(column) {
  return column?.align === 'end' ? 'end' : 'start';
}

export function isSortable(column) {
  return column?.sortable !== false;
}

/** The direction a column sorts in when it is first chosen. */
export function defaultDirection(column) {
  if (column?.defaultDirection === 'asc' || column?.defaultDirection === 'desc')
    return column.defaultDirection;
  return columnAlign(column) === 'end' ? 'desc' : 'asc';
}

/** The sort a header click selects: the active column flips its direction,
 *  another column starts in its default direction. */
export function nextSort(sort, column) {
  if (sort?.column === column.id) {
    return {
      column: column.id,
      direction: sort.direction === 'asc' ? 'desc' : 'asc',
    };
  }
  return { column: column.id, direction: defaultDirection(column) };
}

/** The header cell's `aria-sort`; undefined for a column that cannot sort. */
export function ariaSort(sort, column) {
  if (sort?.column === column.id) {
    return sort.direction === 'asc' ? 'ascending' : 'descending';
  }
  return isSortable(column) ? 'none' : undefined;
}

function columnValue(row, column) {
  return typeof column.sortValue === 'function'
    ? column.sortValue(row)
    : row?.[column.id];
}

function comparable(value) {
  if (value instanceof Date) return value.getTime();
  if (typeof value === 'boolean') return Number(value);
  return value;
}

function isMissing(value) {
  return (
    value === null ||
    value === undefined ||
    (typeof value === 'number' && Number.isNaN(value))
  );
}

function isNumeric(value) {
  return typeof value === 'number' || typeof value === 'bigint';
}

function compareValues(left, right, collator) {
  if (isNumeric(left) && isNumeric(right)) {
    if (left < right) return -1;
    return left > right ? 1 : 0;
  }
  return collator.compare(String(left), String(right));
}

function sortRows(rows, column, direction, locale) {
  const collator = collatorFor(locale);
  const sign = direction === 'asc' ? 1 : -1;
  const entries = rows.map((row, index) => ({
    row,
    index,
    value: comparable(columnValue(row, column)),
  }));
  entries.sort((left, right) => {
    const leftMissing = isMissing(left.value);
    const rightMissing = isMissing(right.value);
    if (leftMissing || rightMissing) {
      if (leftMissing === rightMissing) return left.index - right.index;
      return leftMissing ? 1 : -1;
    }
    return (
      sign * compareValues(left.value, right.value, collator) ||
      left.index - right.index
    );
  });
  return entries.map((entry) => entry.row);
}

// Every whitespace-separated word of the query must occur in the row's text.
function filterRows(rows, query, text, locale) {
  const words = String(query ?? '')
    .trim()
    .toLocaleLowerCase(locale)
    .split(/\s+/)
    .filter(Boolean);
  if (words.length === 0 || typeof text !== 'function') return rows;
  return rows.filter((row) => {
    const haystack = String(text(row) ?? '').toLocaleLowerCase(locale);
    return words.every((word) => haystack.includes(word));
  });
}

/**
 * The rows a table shows: filtered by `query` over `filterText(row)`, sorted
 * by `sort` (`{ column, direction }`, or null for the incoming order), then
 * cut to the first `limit` rows unless `expanded`. `total` counts all rows,
 * `matched` the rows the filter keeps, and `limited` says whether the limit
 * hides rows, i.e. whether a Show all / Show fewer toggle applies.
 */
export function projectRows({
  rows,
  columns = [],
  sort = null,
  query = '',
  filterText = null,
  limit = 0,
  expanded = false,
  locale = 'en',
}) {
  const all = Array.isArray(rows) ? rows : [];
  const matched = filterRows(all, query, filterText, locale);
  const column =
    sort?.column == null
      ? null
      : columns.find((candidate) => candidate.id === sort.column);
  const sorted = column
    ? sortRows(matched, column, sort.direction, locale)
    : matched;
  const limited = limit > 0 && sorted.length > limit;
  return {
    rows: limited && !expanded ? sorted.slice(0, limit) : sorted,
    total: all.length,
    matched: sorted.length,
    limited,
  };
}

/** A count in the locale's number format. */
export function formatCount(value, locale = 'en') {
  return numberFormatFor(locale).format(value);
}

/** A cell's default text: `format(row)` when the column has one, else its
 *  value, with numbers in the locale's format and a dash for missing values. */
export function cellText(row, column, locale = 'en') {
  if (typeof column.format === 'function') return column.format(row);
  const value = columnValue(row, column);
  if (isMissing(comparable(value)) || value === '') return MISSING_TEXT;
  if (isNumeric(value)) return formatCount(value, locale);
  return String(value);
}
