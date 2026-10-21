// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';

import {
  setupChatTimelineSuite,
  timelineSession,
} from './ChatTimeline.support.js';
import {
  assistantRun,
  outputItem,
  reasoningItem,
  toolItem,
} from './timelineItems.support.js';
import { t } from '../../lib/i18n.js';

const WIDGET = {
  id: 'w1',
  title: 'Typography',
  url: '/api/widgets/abc.def',
};

function showWidget(options = {}) {
  return toolItem({
    id: 'call-widget',
    name: 'show_widget',
    arguments: { title: 'Typography', widget_code: '<input name="size">' },
    result: { ok: true },
    display: { version: 1, primary: [], facts: [], widget: WIDGET },
    ...options,
  });
}

describe('ChatTimeline Agent widgets', () => {
  const timeline = setupChatTimelineSuite();

  it('shows a finished widget after its Tool row, outside the collapsed Working group', () => {
    const sessionState = timelineSession();
    sessionState.timelineItems = [
      assistantRun(
        'run-widget',
        [
          reasoningItem('Offer controls'),
          toolItem({
            id: 'call-read',
            name: 'read',
            arguments: { path: 'notes.md' },
            result: { ok: true },
          }),
          showWidget(),
          outputItem('Adjust the values above.'),
        ],
        { status: 'completed' },
      ),
    ];
    timeline.render(sessionState, { chatWorkingMode: 'compact' });

    expect(document.querySelector('.working-block').open).toBe(false);
    const frame = document.querySelector('.chat-widget iframe');
    expect(frame?.getAttribute('src')).toBe(WIDGET.url);
    expect(frame.closest('.working-block')).toBeNull();
    const toolRow = frame
      .closest('.chat-widget')
      .previousElementSibling.querySelector('.te-fn');
    expect(toolRow.textContent).toBe('show_widget');
  });

  it('shows no widget while its call runs', () => {
    const sessionState = timelineSession();
    sessionState.timelineItems = [
      assistantRun('run-widget', [
        showWidget({ result: undefined, status: 'running' }),
      ]),
    ];
    timeline.render(sessionState);

    expect(document.querySelector('.chat-widget')).toBeNull();
  });

  it('records the widget states a send carried, inside a Run and between Runs', () => {
    const widgets = [
      {
        widget_id: 'w1',
        title: 'Typography',
        state: { line_height: 1.6, font_size: 17 },
      },
    ];
    const sessionState = timelineSession();
    sessionState.timelineItems = [
      assistantRun('run-one', [
        { id: 'widget-state-1', type: 'widget_state', widgets, events: [] },
        outputItem('Applied.'),
      ]),
      {
        id: 'widget-state-2',
        type: 'widget_state',
        timestamp: '2026-10-20T10:00:00Z',
        message: {
          id: 'ws2',
          role: 'widget_state',
          timestamp: '2026-10-20T10:00:00Z',
          widgets,
        },
      },
    ];
    timeline.render(sessionState);

    const rows = [...document.querySelectorAll('.widget-state-row')];
    expect(rows).toHaveLength(2);
    expect(rows[0].closest('.assistant-run')).not.toBeNull();
    expect(rows[1].closest('.assistant-run')).toBeNull();
    const summary = rows[0].querySelector('summary').textContent;
    expect(summary).toContain(t('chat.widgetState.label'));
    expect(summary).toContain('Typography');
    expect(summary).toContain('line_height 1.6 · font_size 17');
    expect(
      JSON.parse(
        rows[0].querySelector('.widget-state-row__values').textContent,
      ),
    ).toEqual(widgets[0].state);
  });
});
