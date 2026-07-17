import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";

test("a Chat Run streams a provider response to completion", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page);
  await chat
    .getByRole("textbox", { name: "Message" })
    .fill("E2E_STREAM Please demonstrate streaming");
  await chat.getByRole("button", { name: "Send message" }).click();

  await expect(
    chat.getByText("Fake provider streaming response.", { exact: true }),
  ).toBeVisible();
  await expect(chat.getByText("· Running", { exact: true })).toHaveCount(0);
  await expect(
    chat.getByRole("button", { exact: true, name: "New session" }),
  ).toBeEnabled();
});
