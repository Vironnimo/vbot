import { expect, test } from "@playwright/test";

import { chatTimeline, startIsolatedChat } from "./chat-run-support.js";
import { expectToolSucceeded, runToolScenario } from "./chat-tool-support.js";
import { createAgent, deleteAgentIfPresent } from "./rpc-support.js";

test("a top-level subagent Tool Run delivers its child result automatically", async ({
  page,
  request,
}) => {
  try {
    await createAgent(request, { id: "e2e-worker", name: "E2E Worker" });

    const chat = await startIsolatedChat(page, { agentName: "Main" });
    // The fake Provider answers this only once the Parent's request carries
    // the automatically delivered child answer.
    await runToolScenario(chat, {
      prompt: "E2E_TOOL_SUBAGENT Delegate to the worker",
      finalText: "Sub-agent tool completed.",
      timeout: 45_000,
    });
    const subagent = await expectToolSucceeded(page, chat, "Subagent");
    // The row shows the start result; the child's answer lives in its Session.
    await subagent
      .getByRole("button", { name: "Open Sub-Agent Session" })
      .click();
    await expect(
      chatTimeline(chat).getByText("Fake sub-agent result.", { exact: true }),
    ).toBeVisible();
  } finally {
    await deleteAgentIfPresent(request, "e2e-worker");
  }
});
