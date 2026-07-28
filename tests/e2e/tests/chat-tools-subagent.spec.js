import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import {
  expectToolSucceeded,
  openToolRow,
  runToolScenario,
} from "./chat-tool-support.js";

test("a top-level subagent Tool Run delivers its child result automatically", async ({
  page,
}) => {
  try {
    await page.goto("/#agents");
    const agents = page.getByRole("region", { name: "Agents" });
    const agentList = agents.getByRole("complementary", { name: "Agents" });
    await agentList.getByRole("button", { exact: true, name: "Add" }).click();
    const createDialog = page.getByRole("dialog", { name: "Create agent" });
    await createDialog.getByLabel("Agent ID").fill("e2e-worker");
    await createDialog.getByLabel("Name").fill("E2E Worker");
    await createDialog.getByRole("button", { name: "Create agent" }).click();
    await expect(
      page.getByText("Agent created.", { exact: true }),
    ).toBeVisible();

    const chat = await startIsolatedChat(page, { agentName: "Main" });
    await runToolScenario(chat, {
      prompt: "E2E_TOOL_SUBAGENT Delegate to the worker",
      finalText: "Sub-agent tool completed.",
      timeout: 45_000,
    });
    const subagent = await expectToolSucceeded(page, chat, "Sub-agent");
    await openToolRow(subagent);
    await expect(subagent).toContainText("Fake sub-agent result.");
  } finally {
    await page.request.post("/api/rpc", {
      data: {
        method: "agent.delete",
        params: { id: "e2e-worker" },
      },
    });
  }
});
