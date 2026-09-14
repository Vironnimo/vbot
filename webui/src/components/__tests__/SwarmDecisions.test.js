// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  button,
  createBridge,
  fill,
  render,
  tick,
} from './SwarmPage.support.js';

function decisionsBridge() {
  const fixture = createBridge();
  const original = fixture.operation.getMockImplementation();
  let question = {
    question_id: 'dec-one',
    revision: 3,
    title: '2D or 3D?',
    text: 'Quality first',
    archived: false,
    link: '[Dimensions](#decision/dec-one/3)',
  };
  let options = [
    {
      option_id: 'opt-flat',
      title: '2D',
      text: '',
      support: 3,
      reviewed_support: 3,
    },
    {
      option_id: 'opt-spatial',
      title: '3D',
      text: '',
      support: 0,
      reviewed_support: 0,
    },
  ];
  let position = null;
  fixture.operation.mockImplementation(async (name, args) => {
    if (name === 'board.read')
      return {
        entries: [
          {
            id: 'post-link',
            author: { name: 'Alpha' },
            text: '[Dimensions](#decision/dec-one/1)',
          },
        ],
        has_more: false,
      };
    if (name !== 'decisions') return original(name, args);
    if (args.action === 'list') return { entries: [question], has_more: false };
    if (args.action === 'read')
      return structuredClone({
        question,
        entries: options,
        positions: position ? [position] : [],
        my_position: position,
        position_revision: position?.revision ?? 0,
        participation: { needs_review: 0, not_positioned: 2 },
      });
    if (args.action === 'history') return { entries: [], has_more: false };
    if (
      args.action !== 'create' &&
      args.expected_revision !== question.revision
    )
      throw new Error('test-owned revision conflict');
    if (args.action === 'add_option')
      options.push({
        option_id: 'opt-new',
        title: args.title,
        text: args.text,
        support: 0,
        reviewed_support: 0,
      });
    if (args.action === 'position')
      position = {
        author: { id: 'user', name: 'User' },
        option_id: args.option_id,
        note: args.note,
        revision: (position?.revision ?? 0) + 1,
        question_revision: question.revision,
      };
    else
      question = {
        ...question,
        revision: question.revision + 1,
        ...(args.action === 'update'
          ? { title: args.title, text: args.text }
          : {}),
      };
    return { question };
  });
  return {
    ...fixture,
    peerEdit() {
      question = { ...question, revision: question.revision + 1 };
    },
  };
}

async function openQuestion(fixture) {
  await render(fixture.bridge);
  button('Investigate').click();
  await vi.waitFor(() =>
    expect(
      document.querySelector('a[href="#decision/dec-one/1"]'),
    ).not.toBeNull(),
  );
  document.querySelector('a[href="#decision/dec-one/1"]').click();
  await vi.waitFor(() => expect(button('Add option')).toBeDefined());
}

describe('Swarm decisions', () => {
  it('opens old Board references at the current question and supports a new alternative', async () => {
    const fixture = decisionsBridge();
    await openQuestion(fixture);
    expect(fixture.operation).not.toHaveBeenCalledWith(
      'link.open',
      expect.anything(),
    );
    button('Add option').click();
    await tick();
    fill('decision-title', '2.5D');
    fill('decision-text', 'Prerendered sprites');
    document.getElementById('decision-title').dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Enter',
        bubbles: true,
        cancelable: true,
      }),
    );
    await vi.waitFor(() =>
      expect(document.querySelectorAll('.decision-option')).toHaveLength(3),
    );
    const option = [...document.querySelectorAll('.decision-option')].find(
      (item) => item.textContent.includes('2.5D'),
    );
    [...option.querySelectorAll('button')]
      .find((item) => item.textContent.includes('Support'))
      .click();
    await tick();
    fill('decision-note', 'Better fit for our available assets');
    button('Submit').click();
    await vi.waitFor(() =>
      expect(
        document.querySelector('.decision-position')?.textContent,
      ).toContain('Better fit'),
    );
    expect(fixture.operation).toHaveBeenCalledWith(
      'decisions',
      expect.objectContaining({
        action: 'position',
        option_id: 'opt-new',
        expected_revision: 4,
        position_revision: 0,
      }),
    );
  });

  it('retains conflicting drafts and guards tab changes without publishing a position', async () => {
    const fixture = decisionsBridge();
    await openQuestion(fixture);
    button('Edit question').click();
    await tick();
    fill('decision-title', 'My changed question');
    fixture.peerEdit();
    button('Submit').click();
    await vi.waitFor(() =>
      expect(document.body.textContent).toContain(
        'test-owned revision conflict',
      ),
    );
    expect(document.getElementById('decision-title').value).toBe(
      'My changed question',
    );
    button('Board').click();
    await tick();
    expect(document.querySelector('.decisions-panel')).not.toBeNull();
    button('Discard draft and continue').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.decisions-panel')).toBeNull(),
    );
  });
});
