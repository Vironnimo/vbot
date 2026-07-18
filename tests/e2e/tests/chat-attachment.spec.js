import { Buffer } from "node:buffer";

import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";

test("a text attachment is uploaded, rendered, and reaches provider context", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page);

  await chat.locator('input[type="file"]').setInputFiles({
    name: "e2e-attachment.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("attachment payload 7319\n", "utf8"),
  });
  await expect(
    chat.getByText("e2e-attachment.txt", { exact: true }),
  ).toBeVisible();
  await expect(chat.getByText("Attached file", { exact: true })).toBeVisible();

  await chat
    .getByRole("textbox", { name: "Message" })
    .fill("E2E_ATTACHMENT Inspect the attached text file");
  await chat.getByRole("button", { name: "Send message" }).click();

  await expect(
    chat.getByText("Fake provider received the attachment content.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    chat.getByText("e2e-attachment.txt", { exact: true }),
  ).toBeVisible();
  await expect(chat.getByText("· Running", { exact: true })).toHaveCount(0);
});
