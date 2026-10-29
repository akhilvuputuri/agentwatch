import { expect, test } from "@playwright/test";

test("a trace becomes a dataset item, and items can be added and archived", async ({ page }) => {
  await page.goto("/#/datasets");
  const rows = page.getByTestId("dataset-row");
  await expect(rows.filter({ hasText: "Demo requests" })).toBeVisible();

  const form = page.getByRole("form", { name: "New dataset" });
  await form.getByLabel("Name").fill("Bad answers");
  await form.getByRole("button", { name: "Create dataset" }).click();
  await expect(rows.filter({ hasText: "Bad answers" })).toBeVisible();

  await page.goto("/");
  await page.getByTestId("trace-row").first().getByRole("link").first().click();
  await page.getByRole("button", { name: "Add to dataset" }).click();
  const group = page.getByRole("group", { name: "Add to dataset" });
  await group.getByLabel("Dataset").selectOption({ label: "Bad answers" });
  await group.getByRole("button", { name: "Add", exact: true }).click();
  await expect(group.getByRole("status")).toContainText("Added.");
  await group.getByRole("link", { name: "Open dataset" }).click();

  const items = page.getByTestId("item-table").getByRole("row");
  await expect(items).toHaveCount(2); // header + the trace's item
  await expect(items.nth(1)).toContainText("user:");
  await expect(items.nth(1).getByRole("link", { name: "trace" })).toBeVisible();

  const add = page.getByRole("form", { name: "Add item" });
  await add.getByLabel("Input (JSON or text)").fill('{"question": "2+2"}');
  await add.getByLabel("Expected output").fill("4");
  await add.getByRole("button", { name: "Add item" }).click();
  await expect(items).toHaveCount(3);

  await items.nth(2).getByRole("button", { name: "Archive" }).click();
  await expect(items).toHaveCount(2);
  await page.getByLabel("Show archived").check();
  await expect(items).toHaveCount(3);
});
