import { expect } from "@playwright/test";

export async function runToolScenario(
  chat,
  { finalText, prompt, timeout = 30_000 },
) {
  await chat.getByRole("textbox", { name: "Message" }).fill(prompt);
  await chat.getByRole("button", { name: "Send message" }).click();
  await expect(chat.getByText(finalText, { exact: true })).toBeVisible({
    timeout,
  });
  await expect(chat.getByText("· Running", { exact: true })).toHaveCount(0);
}

export function toolRow(page, chat, name, index = 0) {
  const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return chat
    .locator("details.run-tool-event")
    .filter({
      has: page.locator(".te-fn", {
        hasText: new RegExp(`^${escapedName}$`),
      }),
    })
    .nth(index);
}

export async function expectToolSucceeded(page, chat, name, index = 0) {
  const row = toolRow(page, chat, name, index);
  await expect(row).toBeVisible();
  await expect(row.locator(".te-dot")).toHaveClass(/done/);
  return row;
}

export async function expectToolFailed(page, chat, name, index = 0) {
  const row = toolRow(page, chat, name, index);
  await expect(row).toBeVisible();
  await expect(row.locator(".te-dot")).toHaveClass(/error/);
  return row;
}

export async function openToolRow(row) {
  await row.locator("summary").click();
  await expect(row).toHaveAttribute("open", "");
}
