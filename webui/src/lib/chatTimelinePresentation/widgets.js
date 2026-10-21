// Agent widgets in the timeline: the widget a `show_widget` call shows after
// its row, and the `widget_state` rows that record what a send told the
// Agent about widgets.
import { t } from '$lib/i18n.js';
import { isPlainObject } from '$lib/values.js';
import { toolDisplay, toolNameForRunTool, toolStatus } from './toolFacts.js';
import { trimmedString, truncateToolLabel } from './values.js';

export const SHOW_WIDGET_TOOL_NAME = 'show_widget';
// The same-origin capability path that serves one widget document.
const WIDGET_URL = /^\/api\/widgets\/[A-Za-z0-9._-]+$/;
const MAX_WIDGET_STATE_PREVIEW_LENGTH = 96;

export const isShowWidgetTool = (tool) =>
  toolNameForRunTool(tool) === SHOW_WIDGET_TOOL_NAME;

// The widget a finished `show_widget` call shows, `{ id, title, url }`, or
// null while the call runs or when it showed none. `url` is '' when the
// server delivers no document for it.
export function showWidgetFromTool(tool) {
  if (!isShowWidgetTool(tool) || toolStatus(tool) === 'running') return null;
  const widget = toolDisplay(tool)?.widget;
  const id = trimmedString(widget?.id);
  if (!isPlainObject(widget) || !id) return null;
  return {
    id,
    title: trimmedString(widget.title),
    url:
      typeof widget.url === 'string' && WIDGET_URL.test(widget.url)
        ? widget.url
        : '',
  };
}

// The widgets one `widget_state` row lists: a Run child carries `widgets`
// itself, a History row on its `message`.
export function widgetStateEntries(item) {
  const source = isPlainObject(item?.message) ? item.message : item;
  return (Array.isArray(source?.widgets) ? source.widgets : [])
    .filter(isPlainObject)
    .map((widget) => ({
      widgetId: trimmedString(widget.widget_id),
      title: trimmedString(widget.title),
      state: isPlainObject(widget.state) ? widget.state : {},
    }));
}

function previewValue(value) {
  if (typeof value === 'string') return value.replace(/\s+/g, ' ').trim();
  if (Array.isArray(value)) return `[${value.map(previewValue).join(', ')}]`;
  if (isPlainObject(value)) return JSON.stringify(value);
  return String(value);
}

// One line of a widget state's values, such as `line_height 1.6 ·
// font_size 17`, shortened to fit a row or chip.
export function widgetStatePreview(
  state,
  maxLength = MAX_WIDGET_STATE_PREVIEW_LENGTH,
) {
  const text = Object.entries(isPlainObject(state) ? state : {})
    .map(([key, value]) => `${key} ${previewValue(value)}`)
    .join(' · ');
  return text
    ? truncateToolLabel(text, maxLength)
    : t('chat.widgetState.noValues');
}

// A widget state's values in full, as readable JSON.
export const widgetStateJson = (state) =>
  JSON.stringify(isPlainObject(state) ? state : {}, null, 2);
