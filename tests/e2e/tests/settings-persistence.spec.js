import { expect, test } from "@playwright/test";

test("Appearance settings survive a browser reload", async ({ page }) => {
  await page.goto("/#settings");

  let appearance = page.getByRole("region", { name: "Appearance" });
  const chatWidth = appearance.getByRole("button", { name: "Chat width" });
  await chatWidth.scrollIntoViewIfNeeded();
  await expect(chatWidth).toBeInViewport();
  await chatWidth.click();
  await page.getByRole("option", { name: "Wide" }).click();
  await appearance.getByRole("button", { exact: true, name: "Save" }).click();

  await expect(
    page.getByText("Appearance updated.", { exact: true }),
  ).toBeVisible();

  await page.reload();
  appearance = page.getByRole("region", { name: "Appearance" });
  await expect(
    appearance.getByRole("button", { name: "Chat width" }),
  ).toContainText("Wide");
});
