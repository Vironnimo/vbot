import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";

test("a retryable primary failure switches the Chat Run to its fallback model", async ({
  page,
}) => {
  test.setTimeout(60_000);
  const chat = await startIsolatedChat(page);
  await chat
    .getByRole("textbox", { name: "Message" })
    .fill("E2E_FALLBACK Exercise model fallback");
  await chat.getByRole("button", { name: "Send message" }).click();

  await expect(
    chat.getByText("Switched to fake/e2e-fallback::local", { exact: true }),
  ).toBeVisible({
    timeout: 45_000,
  });
  await expect(
    chat.getByText("Fallback provider response.", { exact: true }),
  ).toBeVisible();
  await expect(chat.getByText("· Running", { exact: true })).toHaveCount(0);
});
