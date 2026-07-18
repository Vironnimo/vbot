import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import {
  expectToolSucceeded,
  runToolScenario,
  toolRow,
} from "./chat-tool-support.js";

test("an Agent Tool allowlist constrains the Provider catalog", async ({
  page,
}) => {
  await page.goto("/#agents");
  const agents = page.getByRole("region", { name: "Agents" });
  const agentList = agents.getByRole("complementary", { name: "Agents" });
  await agentList.getByRole("button", { exact: true, name: "Add" }).click();
  const createDialog = page.getByRole("dialog", { name: "Create agent" });
  await createDialog.getByLabel("Agent ID").fill("tool-restricted");
  await createDialog.getByLabel("Name").fill("Tool Restricted");
  await createDialog.getByRole("button", { name: "Create agent" }).click();
  await expect(page.getByText("Agent created.", { exact: true })).toBeVisible();

  const toolAccess = agents
    .locator(".tl-section")
    .filter({ hasText: "Allowed tools" })
    .first();
  await toolAccess.getByRole("button", { name: "all off" }).click();
  await toolAccess.getByRole("switch", { name: "Toggle tool status" }).click();
  await expect(
    toolAccess.getByRole("switch", { name: "Toggle tool status" }),
  ).toHaveAttribute("aria-checked", "true");
  await agents.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText("Agent updated.", { exact: true })).toBeVisible();

  const chat = await startIsolatedChat(page, { agentName: "Tool Restricted" });
  await runToolScenario(chat, {
    prompt: "E2E_TOOL_CATALOG Verify the restricted Provider Tool catalog",
    finalText: "Restricted Tool catalog verified.",
  });
  await expectToolSucceeded(page, chat, "status");
  await expect(toolRow(page, chat, "write")).toHaveCount(0);
  await expect(toolRow(page, chat, "bash")).toHaveCount(0);

  await page.goto("/#agents");
  await agentList
    .getByRole("button", { name: /^Tool Restricted(?:\s|$)/ })
    .click();
  await agents.getByRole("button", { name: "Delete agent" }).click();
  await expect(page.getByText("Agent deleted.", { exact: true })).toBeVisible();
});
