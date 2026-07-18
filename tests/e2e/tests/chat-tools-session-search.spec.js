import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import {
  expectToolSucceeded,
  openToolRow,
  runToolScenario,
} from "./chat-tool-support.js";

test("session_search finds content persisted by an earlier Run", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page);

  await chat
    .getByRole("textbox", { name: "Message" })
    .fill("E2E_SESSION_SEARCH_SEED Store a unique response");
  await chat.getByRole("button", { name: "Send message" }).click();
  await expect(
    chat.getByText("Stored sapphire beacon 7319.", { exact: true }),
  ).toBeVisible();

  await runToolScenario(chat, {
    prompt: "E2E_TOOL_SESSION_SEARCH Find the seeded response",
    finalText: "Session search tool completed.",
  });
  const search = await expectToolSucceeded(page, chat, "session_search");
  await openToolRow(search);
  await expect(search).toContainText("Stored sapphire beacon 7319.");
});
