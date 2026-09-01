import { expect, test } from "@playwright/test";

const voiceboxPath = process.env.WAYPOINT_VOICEBOX_PATH;

test("Voicebox overview returns ten features with inspectable evidence", async ({
  page,
}) => {
  test.skip(!voiceboxPath, "Set WAYPOINT_VOICEBOX_PATH to run this fixture");

  await page.goto("/");
  await page.getByLabel("Local repository path").fill(voiceboxPath as string);
  const analysisResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/analysis") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Analyze", exact: true }).click();
  expect((await analysisResponse).status()).toBe(200);

  await page.getByRole("button", { name: "ask", exact: true }).click();
  const answerResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith("/answer") &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Send repository question" })
    .click();
  expect((await answerResponse).status()).toBe(200);

  await expect(page.locator(".feature-answer-list li")).toHaveCount(10);
  await expect(page.getByText("Complete privacy", { exact: true })).toBeVisible();
  await expect(page.getByText("Agent voice output", { exact: true })).toBeVisible();
  await expect(page.getByText("README.md", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("package.json", { exact: true }).first()).toBeVisible();
  await expect(
    page.getByText("backend/app.py", { exact: true }).first(),
  ).toBeVisible();
  await page.getByText("Complete privacy", { exact: true }).click();
  await expect(page.locator(".evidence-context code")).toContainText(
    "README.md:L",
  );
  await expect(page.locator(".evidence-source .monaco-editor")).toBeVisible();
  await expect(
    page.locator(".evidence-source .active-line-number").first(),
  ).toHaveText("74");
  await page.screenshot({
    path: "test-results/screenshots/voicebox-ask-split-light.png",
    fullPage: true,
  });
});
