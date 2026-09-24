import { expect, test } from "@playwright/test";

import {
  getAgentPicker,
  sendChatMessage,
  startIsolatedChat,
} from "./chat-run-support.js";
import { createAgent, deleteAgentIfPresent } from "./rpc-support.js";

test("agent moves a Session while handoff starts a fresh cross-Agent Session", async ({
  page,
  request,
}) => {
  try {
    await createAgent(request, {
      id: "e2e-command-agent",
      name: "E2E Command Agent",
    });

    const chat = await startIsolatedChat(page, { agentName: "Main" });
    await sendChatMessage(chat, "E2E_STREAM Session content before move");
    await expect(
      chat.getByText("Fake provider streaming response.", { exact: true }),
    ).toBeVisible();

    await sendChatMessage(
      chat,
      "/agent e2e-command-agent Continue after moving this Session",
    );
    await expect(getAgentPicker(chat)).toContainText("E2E Command Agent");
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
    await expect(getAgentPicker(chat)).toContainText("Main");
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
    await deleteAgentIfPresent(request, "e2e-command-agent");
  }
});
