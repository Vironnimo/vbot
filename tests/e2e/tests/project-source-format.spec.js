import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

import { sendChatMessage } from "./chat-run-support.js";

const projectPath = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "fixtures",
  "mixed-project",
);
const PROJECT_NAME = "Mixed Source E2E Project";

async function removeProject(page) {
  await page.goto("/#projects");
  const projects = page.getByRole("region", { name: "Projects" });
  const projectButton = projects
    .getByRole("complementary", { name: "Projects" })
    .getByRole("button", { name: new RegExp(`^${PROJECT_NAME}(?:\\s|$)`) });
  await expect(projectButton).toBeVisible();
  await projectButton.click();
  await projects.getByRole("button", { exact: true, name: "Remove" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { exact: true, name: "Remove" })
    .click();
  await expect(projects.getByText(/^Project removed\./)).toBeVisible();
}

async function startProjectChat(page, agentName) {
  await page.goto("/#chat");
  const chat = page.getByRole("region", { name: "Chat" });
  await chat.getByRole("button", { name: "Select project" }).click();
  await page.getByRole("option", { exact: true, name: PROJECT_NAME }).click();

  const team = chat.locator(
    '.chat-view__project-team[aria-label="Project team"]',
  );
  await expect(
    team.getByRole("button", { exact: true, name: agentName }),
  ).toBeVisible();
  await team.getByRole("button", { exact: true, name: agentName }).click();

  await chat.getByRole("button", { exact: true, name: "Sessions" }).click();
  const sessionDrawer = chat.getByRole("complementary", { name: "Sessions" });
  const sessionItems = sessionDrawer.getByRole("listitem");
  const emptySessions = sessionDrawer.getByText("No sessions yet", {
    exact: true,
  });
  const currentSession = sessionDrawer.getByText("Current", { exact: true });
  await expect(async () => {
    expect(
      (await emptySessions.isVisible()) || (await currentSession.isVisible()),
    ).toBe(true);
  }).toPass();
  const previousCount = await sessionItems.count();

  await chat.getByRole("button", { exact: true, name: "New session" }).click();
  await expect(sessionItems).toHaveCount(previousCount + 1);
  await expect(
    chat.getByText("No messages yet", { exact: true }).first(),
  ).toBeVisible();
  return chat;
}

test("a Project keeps Source Formats isolated from scan through Provider context", async ({
  page,
}) => {
  test.setTimeout(60_000);
  let projectCreated = false;

  try {
    await page.goto("/#projects");
    const projects = page.getByRole("region", { name: "Projects" });
    const projectList = projects.getByRole("complementary", {
      name: "Projects",
    });
    await projectList.getByRole("button", { exact: true, name: "Add" }).click();

    const addDialog = page.getByRole("dialog", { name: "Add project" });
    await addDialog
      .getByRole("textbox", { name: "Repository path" })
      .fill(projectPath);
    await addDialog
      .getByRole("textbox", { name: "Display name" })
      .fill(PROJECT_NAME);

    const sourceFormats = addDialog.getByRole("radiogroup", {
      name: "Source format",
    });
    await expect(
      sourceFormats.getByRole("radio", { name: /OpenCode.*1 agent.*1 skill/ }),
    ).toBeVisible();
    await expect(
      sourceFormats.getByRole("radio", {
        name: /Claude Code.*1 agent.*1 skill/,
      }),
    ).toBeVisible();
    await sourceFormats.getByRole("radio", { name: /OpenCode/ }).click();
    await addDialog.getByRole("button", { name: "Add project" }).click();

    await expect(
      projects.getByText("Project added.", { exact: true }),
    ).toBeVisible();
    projectCreated = true;
    await expect(
      projects.getByTestId("project-team-member-open-e2e-worker"),
    ).toBeVisible();
    await expect(
      projects.getByTestId("project-team-member-claude-e2e-reviewer"),
    ).toHaveCount(0);
    await expect(
      projects.getByText("open-e2e-skill", { exact: true }),
    ).toBeVisible();
    await expect(
      projects.getByText("claude-e2e-skill", { exact: true }),
    ).toHaveCount(0);

    let chat = await startProjectChat(page, "open-e2e-worker");
    await sendChatMessage(
      chat,
      "E2E_PROJECT_AGENT_CONTEXT Verify the selected repository Agent prompt",
    );
    await expect(
      chat.getByText("OpenCode Project Agent context reached the Provider.", {
        exact: true,
      }),
    ).toBeVisible();

    await page.goto("/#projects");
    const sourceFormat = projects.getByRole("button", {
      exact: true,
      name: "Source format",
    });
    await sourceFormat.click();
    await page
      .getByRole("option", { exact: true, name: "Claude Code" })
      .click();
    await projects.getByRole("button", { name: "Save changes" }).click();
    await expect(
      page.getByText("Project updated.", { exact: true }),
    ).toBeVisible();

    await expect(
      projects.getByTestId("project-team-member-claude-e2e-reviewer"),
    ).toBeVisible();
    await expect(
      projects.getByTestId("project-team-member-open-e2e-worker"),
    ).toHaveCount(0);
    await expect(
      projects.getByText("claude-e2e-skill", { exact: true }),
    ).toBeVisible();
    await expect(
      projects.getByText("open-e2e-skill", { exact: true }),
    ).toHaveCount(0);

    chat = await startProjectChat(page, "claude-e2e-reviewer");
    await sendChatMessage(
      chat,
      "E2E_PROJECT_AGENT_CONTEXT Verify the switched repository Agent prompt",
    );
    await expect(
      chat.getByText("Claude Project Agent context reached the Provider.", {
        exact: true,
      }),
    ).toBeVisible();
  } finally {
    if (projectCreated) {
      await removeProject(page);
    }
  }
});
