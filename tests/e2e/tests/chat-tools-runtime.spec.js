import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import {
  expectToolSucceeded,
  openToolRow,
  runToolScenario,
} from "./chat-tool-support.js";

test("status, bash, and process tools complete through the Agentic Loop", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page);

  await runToolScenario(chat, {
    prompt: "E2E_TOOL_RUNTIME Exercise the runtime tools",
    finalText: "Runtime tools completed.",
  });

  await expectToolSucceeded(page, chat, "status");
  const bash = await expectToolSucceeded(page, chat, "bash");
  await expectToolSucceeded(page, chat, "process");
  await openToolRow(bash);
  await expect(bash).toContainText("e2e-shell-output");
});
