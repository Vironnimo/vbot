import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import { openToolRow, toolRow } from "./chat-tool-support.js";

test("cancelling one running tool lets the Agentic Loop continue", async ({
  page,
}) => {
  test.setTimeout(45_000);
  const chat = await startIsolatedChat(page);

  await chat
    .getByRole("textbox", { name: "Message" })
    .fill("E2E_TOOL_CANCEL Cancel only the long-running tool");
  await chat.getByRole("button", { name: "Send message" }).click();

  const cancelTool = chat.getByRole("button", {
    name: "Cancel running tool call",
  });
  await expect(cancelTool).toBeVisible();
  await cancelTool.click();

  await expect(
    chat.getByText("Tool cancellation handled.", { exact: true }),
  ).toBeVisible({ timeout: 30_000 });
  const bash = toolRow(page, chat, "bash");
  await expect(bash).toBeVisible();
  await expect(bash.locator(".te-dot")).toHaveClass(/cancelled|error/);
  await openToolRow(bash);
  await expect(bash).toContainText("Command aborted by the user");
  await expect(chat.getByText("· Cancelled", { exact: true })).toHaveCount(0);
  await expect(
    chat.getByRole("button", { exact: true, name: "New session" }),
  ).toBeEnabled();
});
