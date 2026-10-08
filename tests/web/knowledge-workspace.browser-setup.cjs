async (page) => {
  await page.unroute("**/api/**");
  const stamp = "2026-09-18T00:00:00Z";
  const base = { domain_id: "mock-domain", account_id: null, created_at: stamp, updated_at: stamp, deleted_at: null, source_type: "manual", source_ref: "", created_by: "mock" };
  const db = {
    cases: [{ ...base, id: "case-mock-0001", title: "模拟案例", content: "签证材料整理经验", platform: "xiaohongshu", author: "测试作者", metrics: {}, url: "", analysis: "", hook: "", structure: "" }],
    assets: [{ ...base, id: "asset-mock-0001", content: "模拟素材正文", summary: "模拟素材", type: "insight", source: "", usable_scene: "", is_verified: false }],
    "user-needs": [{ ...base, id: "need-mock-0001", original_text: "如何准备申请材料", real_problem: "模拟用户需求", user_type: "", scenario: "", demand_type: "", emotion: "" }],
    topics: [{ ...base, id: "topic-mock-0001", title: "模拟选题", topic_description: "材料准备清单", target_user: "", platform: "xiaohongshu", structure_type: "", status: "todo", reason: "" }],
    reviews: [{ ...base, id: "review-mock-0001", summary_text: "模拟复盘", topic_id: "topic-mock-0001", content_item_ref: "", platform: "xiaohongshu", metrics: {}, success_points: "", failure_points: "", improvement: "" }],
    tags: [{ ...base, id: "tag-mock-0001", name: "材料准备", category: "主题", status: "active" }],
    "entity-tags": [],
  };
  const requests = [];
  let sequence = 0;
  await page.route("**/api/**", async route => {
    const request = route.request();
    const method = request.method();
    const url = await page.evaluate(value => { const parsed = new URL(value); return { pathname: parsed.pathname, entity: parsed.searchParams.get("entity_id") }; }, request.url());
    const headers = { "access-control-allow-origin": "*", "access-control-allow-headers": "*", "access-control-allow-methods": "GET,POST,PATCH,OPTIONS" };
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers });
    const body = method === "GET" ? undefined : request.postDataJSON();
    requests.push({ path: url.pathname, method, body });
    const parts = url.pathname.split("/");
    const resource = parts[3];
    const id = parts[4];
    let response = { items: [] };
    if (url.pathname === "/api/kb/topics/recommend") {
      const topic = { ...db.topics[0], id: `recommended-mock-${++sequence}`, title: "模拟推荐选题", status: "todo" };
      db.topics.unshift(topic);
      response = { status: "success", created_count: 1, skipped_count: 0, topics: [topic], source_summary: {} };
    } else if (url.pathname === "/api/kb/import/octopus") {
      const kind = { case: "cases", asset: "assets", user_need: "user-needs", review: "reviews" }[body.entity_type];
      body.items.forEach(item => db[kind].unshift({ ...db[kind][0], ...item, id: `import-mock-${++sequence}` }));
      response = { status: "success", received: body.items.length, success_count: body.items.length, failed_count: 0, failed_items: [], auto_assets_created_count: 0, inserted_ids: [] };
    } else if (parts[2] === "kb" && db[resource]) {
      if (method === "GET") {
        response = { items: db[resource].filter(row => !row.deleted_at && (!url.entity || row.entity_id === url.entity)) };
      } else if (method === "POST") {
        response = { ...base, ...body, id: `${resource}-mock-${++sequence}` };
        db[resource].unshift(response);
      } else if (method === "PATCH") {
        const row = db[resource].find(row => row.id === id);
        Object.assign(row, body, body.deleted !== undefined ? { deleted_at: body.deleted ? stamp : null } : {});
        response = row;
      }
    }
    await route.fulfill({ json: response, headers });
  });
  await page.exposeFunction("kbqaRequests", () => requests);
  await page.goto("http://127.0.0.1:3000/accounts?panel=knowledge");
  await page.getByRole("region", { name: "知识工作台" }).waitFor();
}
