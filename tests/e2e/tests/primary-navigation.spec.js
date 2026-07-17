import { expect, test } from "@playwright/test";

const sections = [
  { button: "Chat", hash: "#chat", region: "Chat" },
  { button: "Agents", hash: "#agents", region: "Agents" },
  { button: "Projects", hash: "#projects", region: "Projects" },
  { button: "Settings", hash: "#settings", region: "Settings" },
  { button: "System Prompt", hash: "#system-prompt", region: "System Prompt" },
  { button: "Cron", hash: "#cron", region: "Cron" },
  { button: "Statistics", hash: "#statistics", region: "Statistics" },
  { button: "Logs", hash: "#logs", region: "Logs" },
];

test("primary navigation opens every available product section", async ({
  page,
}) => {
  await page.goto("/");

  await expect(page).toHaveTitle("vBot");
  await expect(
    page.getByRole("heading", { level: 1, name: "vBot" }),
  ).toBeVisible();
  await expect(page.getByLabel("Connected")).toBeVisible();

  const navigation = page.getByRole("navigation", { name: "Sections" });
  for (const section of sections) {
    await navigation
      .getByRole("button", { exact: true, name: section.button })
      .click();
    await expect(page).toHaveURL(new RegExp(`${section.hash}$`));
    await expect(
      page
        .getByRole("main")
        .getByRole("region", { exact: true, name: section.region }),
    ).toBeVisible();
  }
});
