import { expect, test } from "@playwright/test";

// The second server in playwright.config.ts.
const SIGNED = "http://127.0.0.1:4398";
const OWNER_PASSWORD = "e2e owner password";

test("signs in, returns to the linked trace, and signs out", async ({ page, request }) => {
  const traces = await request.get(`${SIGNED}/api/traces?limit=1`);
  expect(traces.status()).toBe(401);

  // Find a trace to link to: the sign-in page must bring us back to it.
  await page.goto(`${SIGNED}/#/`);
  await expect(page).toHaveURL(/\/login/);
  await page.getByLabel("User name").fill("owner");
  await page.getByLabel("Password").fill("not the password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toHaveText("Wrong user name or password.");

  await page.getByLabel("User name").fill("owner");
  await page.getByLabel("Password").fill(OWNER_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  const rows = page.getByTestId("trace-row");
  await expect(rows.first()).toBeVisible();
  const link = await rows.first().getByRole("link").first().getAttribute("href");
  expect(link).toMatch(/^#\/trace\/[0-9a-f]{32}$/);

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);

  await page.goto(`${SIGNED}/${link}`);
  await expect(page).toHaveURL(new RegExp(`/login${link}$`));
  await page.getByLabel("User name").fill("owner");
  await page.getByLabel("Password").fill(OWNER_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(new RegExp(`/${link}$`));
  await expect(page.getByTestId("span-row").first()).toBeVisible();
});

test("without signing in on, there is no sign-out button", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("trace-row").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign out" })).toHaveCount(0);
});
