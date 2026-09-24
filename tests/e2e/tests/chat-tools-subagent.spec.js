import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import {
  expectToolSucceeded,
  openToolRow,
  runToolScenario,
} from "./chat-tool-support.js";
import { createAgent, deleteAgentIfPresent } from "./rpc-support.js";

test("a top-level subagent Tool Run delivers its child result automatically", async ({
  page,
  request,
}) => {
  try {
    await createAgent(request, { id: "e2e-worker", name: "E2E Worker" });

    const chat = await startIsolatedChat(page, { agentName: "Main" });
    await runToolScenario(chat, {
      prompt: "E2E_TOOL_SUBAGENT Delegate to the worker",
      finalText: "Sub-agent tool completed.",
      timeout: 45_000,
    });
    const subagent = await expectToolSucceeded(page, chat, "Subagent");
    await openToolRow(subagent);
    await expect(subagent).toContainText("Fake sub-agent result.");
  } finally {
    await deleteAgentIfPresent(request, "e2e-worker");
  }
});
