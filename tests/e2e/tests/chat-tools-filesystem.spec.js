import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import {
  expectToolSucceeded,
  openToolRow,
  runToolScenario,
} from "./chat-tool-support.js";

test("filesystem tools create, inspect, edit, and search a workspace file", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page);

  await runToolScenario(chat, {
    prompt: "E2E_TOOL_FILESYSTEM Exercise the workspace tools",
    finalText: "Filesystem tools completed.",
  });

  await expectToolSucceeded(page, chat, "write");
  await expectToolSucceeded(page, chat, "read", 0);
  await expectToolSucceeded(page, chat, "edit");
  await expectToolSucceeded(page, chat, "glob");
  await expectToolSucceeded(page, chat, "grep");
  const finalRead = await expectToolSucceeded(page, chat, "read", 1);
  await openToolRow(finalRead);
  await expect(finalRead).toContainText("needle after");
});
