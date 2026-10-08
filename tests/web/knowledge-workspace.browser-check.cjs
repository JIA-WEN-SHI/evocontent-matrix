async (page) => {
  const region = page.getByRole("region", { name: "知识工作台" });
  if (!await page.evaluate(() => typeof window.kbqaRequests === "function")) throw new Error("Mock session is required");
  if (await page.getByRole("dialog").count()) await page.getByRole("button", { name: "取消", exact: true }).click();
  const cases = [
    { kind: "案例", tab: /^案例/, field: "标题 *", title: "浏览器新增案例" },
    { kind: "素材", tab: /^素材/, field: "素材正文 *", title: "浏览器新增素材" },
    { kind: "用户需求", tab: /^用户需求/, field: "需求原文 *", title: "浏览器新增需求" },
    { kind: "选题", tab: /^选题/, field: "选题标题 *", title: "浏览器新增选题" },
    { kind: "复盘", tab: /^复盘/, field: "复盘摘要 *", title: "浏览器新增复盘" },
  ];
  for (const item of cases) {
    await region.getByRole("tab", { name: item.tab }).click();
    await region.getByRole("button", { name: `新增${item.kind}`, exact: true }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("textbox", { name: item.field, exact: true }).fill(item.title);
    if (item.kind === "复盘") await dialog.getByRole("combobox", { name: "关联选题" }).selectOption({ label: "模拟选题" });
    await dialog.getByRole("button", { name: "保存", exact: true }).click();
    await region.getByRole("button", { name: item.title, exact: true }).waitFor();
    await region.getByRole("button", { name: item.title, exact: true }).click();
    await page.getByRole("dialog").getByRole("textbox", { name: item.field, exact: true }).fill(`${item.title}已编辑`);
    await page.getByRole("dialog").getByRole("button", { name: "保存", exact: true }).click();
    const row = region.getByRole("row").filter({ has: page.getByRole("button", { name: `${item.title}已编辑`, exact: true }) });
    await row.getByRole("button", { name: `删除${item.kind}`, exact: true }).click();
    await page.getByRole("dialog").getByRole("button", { name: "确认删除", exact: true }).click();
    await row.waitFor({ state: "detached" });
  }
  await region.getByRole("tab", { name: /^案例/ }).click();
  await region.getByRole("button", { name: "管理标签", exact: true }).click();
  await page.getByRole("dialog").getByRole("checkbox", { name: "材料准备", exact: true }).check();
  await page.getByRole("dialog").getByRole("checkbox", { name: "材料准备", exact: true }).uncheck();
  await page.getByRole("dialog").getByRole("textbox", { name: "标签名称", exact: true }).fill("浏览器标签");
  await page.getByRole("dialog").getByRole("button", { name: "新增标签", exact: true }).click();
  await page.getByRole("dialog").getByRole("checkbox", { name: "浏览器标签", exact: true }).check();
  await page.getByRole("dialog").getByRole("button", { name: "完成", exact: true }).click();
  await region.getByRole("tab", { name: /^素材/ }).click();
  await region.getByRole("checkbox", { name: "未核实", exact: true }).check();
  await region.getByRole("checkbox", { name: "已核实", exact: true }).waitFor();
  await region.getByRole("tab", { name: /^选题/ }).click();
  await region.getByRole("combobox", { name: "选题状态：模拟选题", exact: true }).selectOption("archived");
  await region.getByRole("combobox", { name: "筛选状态", exact: true }).selectOption("archived");
  await region.getByRole("button", { name: "模拟选题", exact: true }).waitFor();
  await region.getByRole("tab", { name: /^案例/ }).click();
  await region.getByRole("checkbox", { name: "作为选题来源：模拟案例", exact: true }).check();
  await region.getByRole("button", { name: /^推荐选题/ }).click();
  await page.getByRole("dialog").getByRole("button", { name: "生成选题 (1)", exact: true }).click();
  await region.getByRole("button", { name: "模拟推荐选题", exact: true }).waitFor();
  await region.getByRole("button", { name: "导入", exact: true }).click();
  await page.getByRole("dialog").getByRole("textbox", { name: "JSON 内容", exact: true }).fill("bad json");
  if (!await page.getByRole("dialog").getByRole("button", { name: "导入", exact: true }).isDisabled()) throw new Error("Invalid JSON was accepted");
  await page.getByRole("dialog").getByRole("textbox", { name: "JSON 内容", exact: true }).fill('[{"title":"浏览器导入案例","content":"导入正文"}]');
  await page.getByRole("dialog").getByRole("button", { name: "导入 1 条", exact: true }).click();
  await page.getByRole("dialog").getByText("接收 1 条，成功 1 条，失败 0 条。", { exact: true }).waitFor();
  await page.getByRole("dialog").getByRole("button", { name: "完成", exact: true }).click();
  await region.getByRole("button", { name: "浏览器导入案例", exact: true }).waitFor();
  const writes = await page.evaluate(async () => (await window.kbqaRequests()).filter(row => row.method !== "GET"));
  if (!writes.some(row => row.path === "/api/kb/topics/recommend" && row.body.case_ids[0] === "case-mock-0001")) throw new Error("Recommendation source was lost");
  if (!writes.some(row => row.path === "/api/kb/import/octopus" && row.body.items[0].source_ref.startsWith("octopus:manual:"))) throw new Error("Stable import identity missing");
  if (!writes.some(row => row.path === "/api/kb/entity-tags" && row.body.tag_id === "tag-mock-0001")) throw new Error("Tag assignment missing");
  await page.setViewportSize({ width: 1440, height: 960 });
  await region.scrollIntoViewIfNeeded();
  await page.screenshot({ path: "output/playwright/kbqa-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await region.scrollIntoViewIfNeeded();
  const box = await region.boundingBox();
  if (!box || box.width > 390 || box.x < 0) throw new Error("Knowledge workbench overflows mobile viewport");
  await page.screenshot({ path: "output/playwright/kbqa-mobile.png", fullPage: true });
  await region.getByRole("button", { name: "新增案例", exact: true }).click();
  const dialogBox = await page.getByRole("dialog").boundingBox();
  if (!dialogBox || dialogBox.width > 390 || dialogBox.x < 0 || dialogBox.y < 0) throw new Error("Mobile editor is clipped");
  await page.screenshot({ path: "output/playwright/kbqa-mobile-editor.png" });
  await page.getByRole("dialog").getByRole("button", { name: "取消", exact: true }).click();
  console.log(JSON.stringify({ result: "passed", mockWrites: writes.length, checks: ["five create/edit/delete flows", "tag create/assign/unassign", "asset verification", "topic archive/filter", "recommendation sources", "invalid/valid import", "desktop/mobile/editor framing"] }));
}
