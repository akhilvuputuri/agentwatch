import { expect, test } from "@playwright/test";

test("lists demo traces and opens a span tree", async ({ page }) => {
  await page.goto("/");
  const rows = page.getByTestId("trace-row");
  await expect(rows.first()).toBeVisible();
  expect(await rows.count()).toBe(50);

  await page.getByRole("button", { name: "Load older traces" }).click();
  await expect(rows).toHaveCount(100);

  await rows.first().getByRole("link").first().click();
  await expect(page).toHaveURL(/#\/trace\/[0-9a-f]{32}$/);
  const spans = page.getByTestId("span-row");
  await expect(spans.first()).toBeVisible();

  const chat = spans.filter({ hasText: "chat" }).first();
  await chat.click();
  const detail = page.getByTestId("span-detail");
  await expect(detail).toContainText("Input tokens");
  await detail.getByText(/^Attributes \(\d+\)$/).click();
  await expect(detail.getByRole("rowheader", { name: "gen_ai.request.model" })).toBeVisible();

  await page.getByRole("link", { name: "← All traces" }).click();
  await expect(rows.first()).toBeVisible();
});

test("filters to traces with errors, and the address keeps the filter", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Errors only").check();
  const rows = page.getByTestId("trace-row");
  await expect(rows.filter({ hasNotText: "error" })).toHaveCount(0);
  await expect(rows.first()).toBeVisible();
  await expect(page).toHaveURL(/#\/\?errors=1$/);

  // Reopening the address shows the same list; Back undoes the filter.
  await page.reload();
  await expect(page.getByLabel("Errors only")).toBeChecked();
  await expect(rows.first()).toBeVisible();
  await expect(rows.filter({ hasNotText: "error" })).toHaveCount(0);
  await page.getByLabel("Service").selectOption("demo-assistant");
  await expect(page).toHaveURL(/#\/\?service=demo-assistant&errors=1$/);
  await page.goBack();
  await page.goBack();
  await expect(page.getByLabel("Errors only")).not.toBeChecked();

  // A service the store does not have still shows as the filter in force.
  await page.goto("/#/?service=gone");
  await expect(page.getByLabel("Service")).toHaveValue("gone");
  await expect(page.getByText("No traces yet.")).toBeVisible();
  await page.getByLabel("Service").selectOption("");
  await expect(page).toHaveURL(/#\/$/);
  await expect(rows.filter({ hasNotText: "error" }).first()).toBeVisible();
});
