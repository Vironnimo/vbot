import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import {
  expectToolSucceeded,
  runToolScenario,
  toolRow,
} from "./chat-tool-support.js";
import { createAgent, deleteAgentIfPresent } from "./rpc-support.js";

const AGENT_ID = "tool-restricted";
const AGENT_NAME = "Tool Restricted";

test("an Agent Tool allowlist constrains the Provider catalog", async ({
  page,
  request,
}) => {
  try {
    await createAgent(request, { id: AGENT_ID, name: AGENT_NAME });

    await page.goto("/#agents");
    const agents = page.getByRole("region", { name: "Agents" });
    await agents
      .getByRole("complementary", { name: "Agents" })
      .getByRole("button", { name: new RegExp(`^${AGENT_NAME}(?:\\s|$)`) })
      .click();
    await expect(
      agents.getByRole("heading", { level: 2, name: AGENT_NAME }),
    ).toBeVisible();

    const toolAccess = agents.getByRole("region", { name: "Tool access" });
    await toolAccess.getByRole("button", { name: "Deselect all" }).click();
    const status = toolAccess.getByRole("checkbox", {
      exact: true,
      name: "status",
    });
    await status.check();
    await expect(status).toBeChecked();
    await expect(
      toolAccess.getByRole("checkbox", { exact: true, name: "bash" }),
    ).not.toBeChecked();
    await expect(
      toolAccess.getByRole("checkbox", { exact: true, name: "apply_patch" }),
    ).not.toBeChecked();
    await expect(
      agents.getByRole("button", { exact: true, name: "Saved" }),
    ).toBeVisible();

    const chat = await startIsolatedChat(page, { agentName: AGENT_NAME });
    await runToolScenario(chat, {
      prompt: "E2E_TOOL_CATALOG Verify the restricted Provider Tool catalog",
      finalText: "Restricted Tool catalog verified.",
    });
    await expectToolSucceeded(page, chat, "status");
    await expect(toolRow(page, chat, "apply_patch")).toHaveCount(0);
    await expect(toolRow(page, chat, "bash")).toHaveCount(0);
  } finally {
    await deleteAgentIfPresent(request, AGENT_ID);
  }
});
