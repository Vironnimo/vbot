import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

const projectPath = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);

test("a Project can be created, renamed, and removed", async ({ page }) => {
  await page.goto("/#projects");

  const projects = page.getByRole("region", { name: "Projects" });
  const projectList = projects.getByRole("complementary", { name: "Projects" });

  await projectList.getByRole("button", { exact: true, name: "Add" }).click();

  const addDialog = page.getByRole("dialog", { name: "Add project" });
  await addDialog
    .getByRole("textbox", { name: "Repository path" })
    .fill(projectPath);
  await addDialog
    .getByRole("textbox", { name: "Display name" })
    .fill("E2E Project");
  await addDialog.getByRole("button", { name: "Add project" }).click();

  await expect(
    projects.getByText("Project added.", { exact: true }),
  ).toBeVisible();
  await expect(
    projectList.getByText("E2E Project", { exact: true }),
  ).toBeVisible();
  await expect(
    projects.getByText(projectPath, { exact: true }).first(),
  ).toBeVisible();

  await projects
    .getByRole("textbox", { exact: true, name: "Display name" })
    .fill("E2E Project Updated");
  await projects.getByRole("button", { name: "Save changes" }).click();

  await expect(
    projectList.getByText("E2E Project Updated", { exact: true }),
  ).toBeVisible();

  await projects.getByRole("button", { exact: true, name: "Remove" }).click();

  const removeDialog = page.getByRole("dialog");
  await expect(
    removeDialog.getByRole("heading", { name: "Remove project" }),
  ).toBeVisible();
  await expect(removeDialog).toContainText("E2E Project Updated");
  await removeDialog
    .getByRole("button", { exact: true, name: "Remove" })
    .click();

  await expect(projects.getByText(/^Project removed\./)).toBeVisible();
  await expect(
    projects.getByText("No projects yet", { exact: true }),
  ).toBeVisible();
  await expect(
    projectList.getByText("E2E Project Updated", { exact: true }),
  ).toHaveCount(0);
});
