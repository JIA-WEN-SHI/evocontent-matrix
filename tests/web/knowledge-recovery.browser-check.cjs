// Run after knowledge-workspace.browser-setup.cjs in the isolated mock session.
async (page) => {
  if (!await page.evaluate(() => typeof window.kbqaRequests === "function")) throw new Error("Mock session required");
  const region = page.getByRole("region", { name: "知识工作台" });
  const dialog = page.getByRole("dialog");
  const title = `加载失败后的新增案例 ${Date.now()}`;
  if (await dialog.count()) await dialog.getByRole("button", { name: "完成", exact: true }).click();
  await region.getByRole("tab", { name: /^案例/ }).click();
  let failRead = true;
  const intercept = async route => {
    if (route.request().method() === "GET" && failRead) {
      failRead = false;
      return route.fulfill({ status: 503, headers: { "access-control-allow-origin": "*" }, json: { detail: "模拟列表加载失败" } });
    }
    return route.fallback();
  };
  await page.route("**/api/kb/cases?*", intercept);
  try {
    await region.getByRole("button", { name: "刷新列表", exact: true }).click();
    await region.getByRole("button", { name: "重新加载", exact: true }).waitFor();
    await region.getByRole("button", { name: "新增案例", exact: true }).click();
    await dialog.getByRole("textbox", { name: "标题 *", exact: true }).fill(title);
    await dialog.getByRole("button", { name: "保存", exact: true }).click();
    await dialog.waitFor({ state: "hidden" });
    await region.getByRole("button", { name: title, exact: true }).waitFor({ timeout: 3000 });
    if (await region.getByRole("button", { name: "重新加载", exact: true }).count()) throw new Error("A successful create must clear stale list errors");
    await region.getByRole("row").filter({ has: page.getByRole("button", { name: title, exact: true }) }).getByRole("button", { name: "删除案例", exact: true }).click();
    await dialog.getByRole("button", { name: "确认删除", exact: true }).click();
    await dialog.waitFor({ state: "hidden" });
    return { passed: true, checks: ["create recovers list after failed refresh"] };
  } finally { await page.unroute("**/api/kb/cases?*", intercept); }
}
