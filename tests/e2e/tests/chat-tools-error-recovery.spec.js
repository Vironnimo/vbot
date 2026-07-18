import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import {
  expectToolFailed,
  openToolRow,
  runToolScenario,
} from "./chat-tool-support.js";

test("an expected tool failure stays visible and the Agentic Loop recovers", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page);

  await runToolScenario(chat, {
    prompt: "E2E_TOOL_MISSING_FILE Exercise tool failure recovery",
    finalText: "Missing file error handled.",
  });

  const read = await expectToolFailed(page, chat, "read");
  await openToolRow(read);
  await expect(read).toContainText(/file_not_found|File not found/i);
  await expect(
    chat.getByRole("button", { exact: true, name: "New session" }),
  ).toBeEnabled();
});
