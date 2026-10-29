import { expect, test } from "@playwright/test";

test("runs show on the dataset and open to their items", async ({ page }) => {
  await page.goto("/#/datasets");
  await page.getByTestId("dataset-row").filter({ hasText: "Demo requests" }).getByRole("link").click();
  const runs = page.getByTestId("run-table").getByRole("row");
  await expect(runs).toHaveCount(3); // header + baseline + prompt v2
  await expect(page.getByTestId("run-table").getByRole("columnheader", { name: "exact" })).toBeVisible();

  await runs.filter({ hasText: "prompt v2" }).getByRole("link").click();
  await expect(page.getByRole("heading", { name: "Run prompt v2" })).toBeVisible();
  const items = page.getByTestId("run-items").getByRole("row");
  await expect(items).toHaveCount(7);
  await expect(items.nth(1)).toContainText("exact");

  await items.nth(1).getByRole("link", { name: "trace" }).click();
  await expect(page).toHaveURL(/#\/trace\/[0-9a-f]{32}$/);
  await expect(page.getByRole("heading", { level: 1, name: "experiment prompt v2" })).toBeVisible();
});
