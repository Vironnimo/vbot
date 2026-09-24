import { expect, test } from "@playwright/test";

import { sendChatMessage, startIsolatedChat } from "./chat-run-support.js";

const PROMPT = "E2E_SLOW Follow this Run across a reload and a second tab";
// The fake Provider streams one opening chunk plus 80 continuation chunks.
const CONTINUATION_CHUNKS = 80;

function assistantAnswer(chat) {
  return chat
    .getByRole("article")
    .filter({ hasText: "Slow response started." })
    .getByRole("paragraph");
}

async function streamedChunkCount(chat) {
  const text = await assistantAnswer(chat).innerText();
  return text.match(/still running/g)?.length ?? 0;
}

test("a running Chat Run survives a reload and streams live into a second tab", async ({
  context,
  page,
}) => {
  test.setTimeout(60_000);
  const chat = await startIsolatedChat(page);
  await sendChatMessage(chat, PROMPT);
  await expect(chat.getByText(/Slow response started\./)).toBeVisible();

  // A fresh page load has no client state: it must reattach to the server-owned
  // Run and keep streaming instead of showing a stale or empty answer.
  await page.reload();
  const reloadedChat = page.getByRole("region", { name: "Chat" });
  await expect(reloadedChat.getByText(PROMPT, { exact: true })).toHaveCount(1);
  await expect(
    reloadedChat.getByRole("button", { name: "Cancel run" }),
  ).toBeVisible();
  const chunksAfterReload = await streamedChunkCount(reloadedChat);
  expect(chunksAfterReload).toBeLessThan(CONTINUATION_CHUNKS);
  await expect
    .poll(() => streamedChunkCount(reloadedChat))
    .toBeGreaterThan(chunksAfterReload);

  // Another client opening the same Agent follows the same live Run.
  const secondPage = await context.newPage();
  await secondPage.goto("/#chat");
  const secondChat = secondPage.getByRole("region", { name: "Chat" });
  await expect(secondChat.getByText(PROMPT, { exact: true })).toHaveCount(1);
  await expect(
    secondChat.getByRole("button", { name: "Cancel run" }),
  ).toBeVisible();

  // Both clients converge on the one completed answer: nothing duplicated
  // from replay, nothing lost across the reload.
  for (const view of [reloadedChat, secondChat]) {
    await expect(view.getByRole("button", { name: "Cancel run" })).toHaveCount(
      0,
      { timeout: 30_000 },
    );
    await expect(view.getByText(PROMPT, { exact: true })).toHaveCount(1);
    await expect(assistantAnswer(view)).toHaveCount(1);
    await expect.poll(() => streamedChunkCount(view)).toBe(CONTINUATION_CHUNKS);
  }
  await secondPage.close();
});
