import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import {
  expectToolSucceeded,
  openToolRow,
  runToolScenario,
} from "./chat-tool-support.js";

test("a foreground subagent Tool Run returns its child result to the parent", async ({
  page,
}) => {
  await page.goto("/#agents");
  const agents = page.getByRole("region", { name: "Agents" });
  const agentList = agents.getByRole("complementary", { name: "Agents" });
  await agentList.getByRole("button", { exact: true, name: "Add" }).click();
  const createDialog = page.getByRole("dialog", { name: "Create agent" });
  await createDialog.getByLabel("Agent ID").fill("e2e-worker");
  await createDialog.getByLabel("Name").fill("E2E Worker");
  await createDialog.getByRole("button", { name: "Create agent" }).click();
  await expect(page.getByText("Agent created.", { exact: true })).toBeVisible();

  const chat = await startIsolatedChat(page, { agentName: "Main" });
  await runToolScenario(chat, {
    prompt: "E2E_TOOL_SUBAGENT Delegate to the worker",
    finalText: "Sub-agent tool completed.",
    timeout: 45_000,
  });
  const subagent = await expectToolSucceeded(page, chat, "Sub-agent");
  await openToolRow(subagent);
  await expect(subagent).toContainText("Fake sub-agent result.");
  const result = await expectToolSucceeded(page, chat, "Sub-agent", 1);
  await openToolRow(result);
  await expect(result).toContainText("Fake sub-agent result.");

  await page.goto("/#agents");
  await agentList.getByRole("button", { name: /^E2E Worker(?:\s|$)/ }).click();
  await agents.getByRole("button", { name: "Delete agent" }).click();
  await expect(page.getByText("Agent deleted.", { exact: true })).toBeVisible();
});
