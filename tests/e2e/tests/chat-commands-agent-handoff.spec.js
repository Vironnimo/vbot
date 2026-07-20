import { expect, test } from "@playwright/test";

import {
  getAgentTab,
  sendChatMessage,
  startIsolatedChat,
} from "./chat-run-support.js";

test("agent moves a Session while handoff starts a fresh cross-Agent Session", async ({
  page,
}) => {
  await page.goto("/#agents");
  const agents = page.getByRole("region", { name: "Agents" });
  const agentList = agents.getByRole("complementary", { name: "Agents" });
  await agentList.getByRole("button", { exact: true, name: "Add" }).click();
  const createDialog = page.getByRole("dialog", { name: "Create agent" });
  await createDialog.getByLabel("Agent ID").fill("e2e-command-agent");
  await createDialog.getByLabel("Name").fill("E2E Command Agent");
  await createDialog.getByRole("button", { name: "Create agent" }).click();
  await expect(page.getByText("Agent created.", { exact: true })).toBeVisible();

  try {
    const chat = await startIsolatedChat(page, { agentName: "Main" });
    await sendChatMessage(chat, "E2E_STREAM Session content before move");
    await expect(
      chat.getByText("Fake provider streaming response.", { exact: true }),
    ).toBeVisible();

    await sendChatMessage(
      chat,
      "/agent e2e-command-agent Continue after moving this Session",
    );
    await expect(getAgentTab(chat, "E2E Command Agent")).toBeVisible();
    await expect(
      chat
        .getByRole("article")
        .filter({ hasText: "E2E_STREAM Session content before move" }),
    ).toBeVisible();
    await expect(
      chat.getByText("Fake provider response.", { exact: true }),
    ).toBeVisible();

    await sendChatMessage(
      chat,
      "/handoff agent:main Preserve the deterministic E2E handoff brief",
    );
    await expect(getAgentTab(chat, "Main")).toBeVisible();
    await expect(
      chat
        .getByRole("article")
        .filter({ hasText: "E2E handoff brief 9182." })
        .getByText("E2E handoff brief 9182.", { exact: true }),
    ).toBeVisible();
    await expect(
      chat.getByText("E2E handoff received by target Agent.", { exact: true }),
    ).toBeVisible();
  } finally {
    await page.goto("/#agents");
    await agentList
      .getByRole("button", { name: /^E2E Command Agent(?:\s|$)/ })
      .click();
    await agents.getByRole("button", { name: "Delete agent" }).click();
    await expect(
      page.getByText("Agent deleted.", { exact: true }),
    ).toBeVisible();
  }
});
