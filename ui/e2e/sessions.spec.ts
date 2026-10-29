import { expect, test } from "@playwright/test";

test("sessions list, session timeline and filtered trace list", async ({ page }) => {
  await page.goto("/#/sessions");
  const rows = page.getByTestId("session-row");
  await expect(rows.first()).toBeVisible();
  await rows.first().getByRole("link").first().click();
  await expect(page).toHaveURL(/#\/session\/demo-\d$/);
  const timeline = page.getByTestId("session-traces").getByRole("listitem");
  await expect(timeline.first()).toBeVisible();
  const traces = await timeline.count();

  await page.getByRole("link", { name: "Open in trace list" }).click();
  await expect(page).toHaveURL(/#\/\?session=demo-\d$/);
  await expect(page.getByTestId("trace-row")).toHaveCount(Math.min(traces, 50));
  await expect(page.getByText(/^Session/).first()).toBeVisible();

  await page.getByTestId("trace-row").first().getByRole("link").first().click();
  await expect(page).toHaveURL(/#\/trace\/[0-9a-f]{32}$/);
  await expect(page.getByRole("link", { name: /^session demo-/ })).toHaveCount(1);
});
