import { expect, test } from "@playwright/test";

test("Projects presents a useful empty state without configuration", async ({
  page,
}) => {
  await page.goto("/#projects");

  const projects = page.getByRole("region", { name: "Projects" });
  await expect(
    projects.getByText("No projects yet", { exact: true }),
  ).toBeVisible();
  await expect(
    projects.getByRole("button", { name: "Add", exact: true }),
  ).toBeVisible();
});
