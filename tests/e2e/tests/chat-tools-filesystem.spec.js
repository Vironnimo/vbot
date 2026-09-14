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

  await expectToolSucceeded(page, chat, "apply_patch", 0);
  await expectToolSucceeded(page, chat, "read", 0);
  await expectToolSucceeded(page, chat, "apply_patch", 1);
  const paths = await expectToolSucceeded(page, chat, "search_files", 0);
  await openToolRow(paths);
  await expect(paths).toContainText("workflow.txt");
  const content = await expectToolSucceeded(page, chat, "search_files", 1);
  await openToolRow(content);
  await expect(content).toContainText("needle after");
  const finalRead = await expectToolSucceeded(page, chat, "read", 1);
  await openToolRow(finalRead);
  await expect(finalRead).toContainText("needle after");
});
