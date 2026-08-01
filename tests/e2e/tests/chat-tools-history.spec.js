import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import {
  expectToolSucceeded,
  openToolRow,
  runToolScenario,
} from "./chat-tool-support.js";

test("Compaction grants lossless access to original Session history", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page);

  await chat
    .getByRole("textbox", { name: "Message" })
    .fill("E2E_HISTORY_SEED Store the deterministic archive marker");
  await chat.getByRole("button", { name: "Send message" }).click();
  await expect(
    chat.getByText("Archived obsidian record 8642.", { exact: true }),
  ).toBeVisible();

  await chat.getByRole("textbox", { name: "Message" }).fill("/compact");
  await chat.getByRole("button", { name: "Send message" }).click();
  await expect(chat.getByText(/^Context compacted/)).toBeVisible({
    timeout: 30_000,
  });

  await runToolScenario(chat, {
    prompt: "E2E_TOOL_HISTORY Recover the original archived response",
    finalText: "Compacted Session history recovered.",
  });
  const history = await expectToolSucceeded(page, chat, "history");
  await openToolRow(history);
  await expect(history).toContainText("Archived obsidian record 8642.");
});
