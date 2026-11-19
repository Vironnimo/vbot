// @vitest-environment jsdom

import { expect, it, vi } from 'vitest';

import { createSideChat, startChatRun } from '../api.js';
import { createAppChats } from '../appChats.svelte.js';

vi.mock('../api.js', () => ({
  createSideChat: vi.fn(),
  startChatRun: vi.fn(),
}));

it('keeps a side draft and its fork under the current Identity Agent through rename', async () => {
  const chats = createAppChats(() => 'chat');
  const draft = (agent_id) => ({
    address: { agent_id, session_id: 'parent' },
    sourceGenerationId: 'parent-generation',
  });
  chats.openSideDraft(draft('old'));
  const launchId = chats.popup.launch.id;
  chats.close();
  chats.renameAgent('old', 'renamed');
  chats.openSideDraft(draft('renamed'));
  expect(chats.popup.launch.id).toBe(launchId);

  const fork = Promise.withResolvers();
  const send = Promise.withResolvers();
  vi.mocked(createSideChat).mockReturnValue(fork.promise);
  vi.mocked(startChatRun).mockReturnValue(send.promise);
  const result = chats.popup.sendFirst('question');
  await Promise.resolve();
  expect(createSideChat).toHaveBeenCalledWith(
    'renamed',
    'parent',
    'parent-generation',
    launchId,
  );

  chats.renameAgent('renamed', 'while-forking');
  fork.resolve({
    agent_id: 'renamed',
    session_id: 'child',
    generation_id: 'child-generation',
  });
  await vi.waitFor(() =>
    expect(startChatRun).toHaveBeenCalledWith(
      expect.objectContaining({
        agent_id: 'while-forking',
        session_id: 'child',
        expected_generation_id: 'child-generation',
      }),
    ),
  );
  chats.renameAgent('while-forking', 'while-sending');
  send.resolve({ accepted: true });
  expect(await result).toMatchObject({
    address: { agent_id: 'while-sending', session_id: 'child' },
    draftKey: 'while-sending::child',
  });

  chats.renameAgent('while-sending', 'after-send');
  expect(chats.popup.launch.address.agent_id).toBe('after-send');
});
