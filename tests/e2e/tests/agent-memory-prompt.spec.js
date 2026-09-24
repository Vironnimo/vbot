import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import { expectToolSucceeded, runToolScenario } from "./chat-tool-support.js";
import { createAgent, deleteAgentIfPresent } from "./rpc-support.js";

const AGENT_ID = "memory-prompt-agent";
const AGENT_NAME = "Memory Prompt Agent";

async function setMemoryMode(page, modeName) {
  await page.goto("/#agents");
  const agents = page.getByRole("region", { name: "Agents" });
  await agents
    .getByRole("complementary", { name: "Agents" })
    .getByRole("button", { name: new RegExp(`^${AGENT_NAME}(?:\\s|$)`) })
    .click();

  const context = agents.getByRole("region", { name: "Context & Memory" });
  const memoryMode = context.getByRole("button", {
    exact: true,
    name: "Memory",
  });
  await memoryMode.scrollIntoViewIfNeeded();
  await memoryMode.click();
  await page.getByRole("option", { exact: true, name: modeName }).click();
  await expect(memoryMode).toContainText(modeName);
  await expect(
    agents.getByRole("button", { exact: true, name: "Saved" }),
  ).toBeVisible();
}

test("Agent Memory settings control what reaches the Provider prompt", async ({
  page,
  request,
}) => {
  try {
    await createAgent(request, { id: AGENT_ID, name: AGENT_NAME });

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
    await deleteAgentIfPresent(request, AGENT_ID);
  }
});
