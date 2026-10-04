import { expect, test } from "@playwright/test";

import { chatTimeline, startIsolatedChat } from "./chat-run-support.js";

test("Manual Compaction compacts the conversation", async ({ page }) => {
  const chat = await startIsolatedChat(page);

  await chat
    .getByRole("textbox", { name: "Message" })
    .fill("E2E_COMPACTION_SEED Store the deterministic archive marker");
  await chat.getByRole("button", { name: "Send message" }).click();
  await expect(
    chatTimeline(chat).getByText("Archived obsidian record 8642.", {
      exact: true,
    }),
  ).toBeVisible();

  await chat.getByRole("textbox", { name: "Message" }).fill("/compact");
  await chat.getByRole("button", { name: "Send message" }).click();
  await expect(chat.getByText(/^Context compacted/)).toBeVisible({
    timeout: 30_000,
  });
});
