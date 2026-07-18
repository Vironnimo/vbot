import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";

test("a queued Chat message can be edited and runs after the active Run", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page);
  const messageInput = chat.getByRole("textbox", { name: "Message" });

  await messageInput.fill("E2E_QUEUE_ACTIVE Keep this Run active");
  await chat.getByRole("button", { name: "Send message" }).click();
  await expect(chat.getByText(/Queue run started\./)).toBeVisible();

  await messageInput.fill("E2E_QUEUE_FOLLOWUP Original queued message");
  await chat.getByRole("button", { name: "Queue message" }).click();

  const queue = chat.getByRole("complementary", {
    name: "Queued messages",
  });
  await expect(queue.getByText("1 queued", { exact: true })).toBeVisible();
  await expect(
    queue.getByText("E2E_QUEUE_FOLLOWUP Original queued message", {
      exact: true,
    }),
  ).toBeVisible();

  await queue.getByRole("button", { name: "Edit queued message" }).click();
  await queue
    .getByRole("textbox")
    .fill("E2E_QUEUE_FOLLOWUP Edited queued message");
  await queue.getByRole("button", { exact: true, name: "Save" }).click();

  await expect(
    queue.getByText("E2E_QUEUE_FOLLOWUP Edited queued message", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(queue).toHaveCount(0, { timeout: 15_000 });
  await expect(
    chat.getByText("E2E_QUEUE_FOLLOWUP Edited queued message", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    chat.getByText("Fake provider queued response.", { exact: true }),
  ).toBeVisible();
  await expect(chat.getByText("· Running", { exact: true })).toHaveCount(0);
});
