import { expect, test } from "@playwright/test";

test("scores show in the list, can be annotated and deleted on a trace", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".score-badge").first()).toBeVisible();

  await page.getByTestId("trace-row").first().getByRole("link").first().click();
  await expect(page).toHaveURL(/#\/trace\/[0-9a-f]{32}$/);
  const panel = page.getByRole("region", { name: "Scores" });
  await panel.getByRole("button", { name: "Add score" }).click();
  await panel.getByLabel("Name").fill("reviewer_rating");
  await panel.getByLabel("Value").fill("4");
  await panel.getByLabel("Comment").fill("checked by hand");
  await panel.getByRole("button", { name: "Save" }).click();

  const row = page.getByTestId("score-table").getByRole("row", { name: /reviewer_rating/ });
  await expect(row).toContainText("4");
  await expect(row).toContainText("annotation");

  // Still there after a reload, then gone after deleting.
  await page.reload();
  await expect(page.getByTestId("score-table").getByRole("row", { name: /reviewer_rating/ })).toBeVisible();
  page.once("dialog", (dialog) => void dialog.accept());
  await page.getByRole("button", { name: "Delete score reviewer_rating" }).click();
  await expect(page.getByTestId("score-table").getByRole("row", { name: /reviewer_rating/ })).toHaveCount(0);
});

test("dashboard lists scores", async ({ page }) => {
  await page.goto("/#/dashboard");
  await page.getByRole("button", { name: "All time" }).click();
  await expect(page.getByRole("cell", { name: "helpfulness" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "topic" })).toBeVisible();
});
