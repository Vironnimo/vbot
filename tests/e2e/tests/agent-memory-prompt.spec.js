import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import { expectToolSucceeded, runToolScenario } from "./chat-tool-support.js";

const AGENT_ID = "memory-prompt-agent";
const AGENT_NAME = "Memory Prompt Agent";

async function setMemoryMode(page, modeName) {
  await page.goto("/#agents");
  const agents = page.getByRole("region", { name: "Agents" });
  const agentList = agents.getByRole("complementary", { name: "Agents" });
  await agentList
    .getByRole("button", { name: new RegExp(`^${AGENT_NAME}(?:\\s|$)`) })
    .click();

  const memoryMode = agents.getByRole("button", {
    exact: true,
    name: "Memory",
  });
  await memoryMode.scrollIntoViewIfNeeded();
  await expect(memoryMode).toBeInViewport();
  await memoryMode.click();
  await page.getByRole("option", { exact: true, name: modeName }).click();
  await agents.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText("Agent updated.", { exact: true })).toBeVisible();
}

async function deleteAgent(page) {
  await page.goto("/#agents");
  const agents = page.getByRole("region", { name: "Agents" });
  const agentButton = agents
    .getByRole("complementary", { name: "Agents" })
    .getByRole("button", { name: new RegExp(`^${AGENT_NAME}(?:\\s|$)`) });
  await expect(agentButton).toBeVisible();
  await agentButton.click();
  await agents.getByRole("button", { name: "Delete agent" }).click();
  await expect(page.getByText("Agent deleted.", { exact: true })).toBeVisible();
}

test("Agent Memory settings control what reaches the Provider prompt", async ({
  page,
}) => {
  let agentCreated = false;
  try {
    await page.goto("/#agents");
    const agents = page.getByRole("region", { name: "Agents" });
    await agents
      .getByRole("complementary", { name: "Agents" })
      .getByRole("button", { exact: true, name: "Add" })
      .click();

    const createDialog = page.getByRole("dialog", { name: "Create agent" });
    await createDialog.getByLabel("Agent ID").fill(AGENT_ID);
    await createDialog.getByLabel("Name").fill(AGENT_NAME);
    await createDialog.getByRole("button", { name: "Create agent" }).click();
    await expect(
      page.getByText("Agent created.", { exact: true }),
    ).toBeVisible();
    agentCreated = true;

    let chat = await startIsolatedChat(page, { agentName: AGENT_NAME });
    await runToolScenario(chat, {
      prompt: "E2E_MEMORY_PROMPT_SEED Store deterministic pinned Memory",
      finalText: "Memory prompt seed stored.",
    });
    await expectToolSucceeded(page, chat, "memory");

    await setMemoryMode(page, "Off");
    chat = await startIsolatedChat(page, { agentName: AGENT_NAME });
    await runToolScenario(chat, {
      prompt: "E2E_MEMORY_PROMPT_CHECK Check the disabled Memory context",
      finalText: "Pinned Memory stayed out of the Provider prompt.",
    });

    await setMemoryMode(page, "Agent notes (MEMORY.md)");
    chat = await startIsolatedChat(page, { agentName: AGENT_NAME });
    await runToolScenario(chat, {
      prompt: "E2E_MEMORY_PROMPT_CHECK Check the enabled Memory context",
      finalText: "Pinned Memory reached the Provider.",
    });
  } finally {
    if (agentCreated) {
      await deleteAgent(page);
    }
  }
});
