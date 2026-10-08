import type {
  CoachAction,
  CoachBrief,
  OpsOverview,
  PendingStrategyItem,
  PipelineTask,
  PublishFeedbackSummaryItem,
  RuntimeContext,
} from "@/lib/types";

type LandingDemoFixture = {
  runtime: RuntimeContext;
  overviewGlobal: OpsOverview;
  overviewAccount: OpsOverview;
  publishSummary: PublishFeedbackSummaryItem[];
  pendingStrategy: PendingStrategyItem[];
  pendingTasks: PipelineTask[];
  coachBrief: CoachBrief;
  coachActions: CoachAction[];
};

function isoOffset(base: Date, minutes: number): string {
  return new Date(base.getTime() + minutes * 60 * 1000).toISOString();
}

export function buildLandingDemoFixture(): LandingDemoFixture {
  const now = new Date();
  const domainId = "demo-domain-id";
  const accountId = "demo-account-id";
  const accountName = "日本移民增长演示号";

  const runtime: RuntimeContext = {
    status: "demo_fallback",
    domain_slug: "japan_immigration",
    publish_runtime: {
      mode: "demo",
      dry_run: true,
      ready_for_real_publish: false,
      blockers: [],
      next_actions: [],
    },
    publish_account: {
      configured: true,
      account_id: accountId,
      account_name: accountName,
      account_handle: "@demo_japan_growth",
      login_mode: "storage_state",
      auth_hint: "demo.storage.json",
      strategy_profile: {
        persona_name: "日本移民实操顾问",
        ip_positioning: "给家庭用户做日本身份与路径规划",
        tone_style: "理性、可执行、反焦虑",
        primary_goal: "曝光优先",
      },
      last_login_check_status: "ok",
      last_login_check_at: isoOffset(now, -30),
      last_login_check_message: "Demo mode",
    },
    crawler_account: {
      source: "demo",
      auth_hint: "demo",
      xhs_enabled: true,
      xhs_max_per_query: 8,
      google_enabled: true,
      google_max_per_query: 8,
    },
    latest: {
      last_xhs_intel_at: isoOffset(now, -25),
      last_google_hotspot_at: isoOffset(now, -33),
      last_google_viewpoint_at: isoOffset(now, -52),
      last_draft_task_id: "demo-pending-1",
      last_draft_task_status: "pending_review",
      last_published_task_id: "demo-published-1",
      last_published_at: isoOffset(now, -1380),
      last_task_account_id: accountId,
      last_task_account_name: accountName,
    },
  };

  const publicRefs = [
    "示例选题：日本高度人才签证常见问题",
    "示例选题：经营管理签证材料准备",
    "示例选题：赴日工作的读者疑问",
    "示例选题：永住申请资料核对",
    "示例选题：留学与工作路径比较",
    "示例选题：不同身份路径的适用条件",
    "示例选题：材料清单型内容",
    "示例选题：预算问题的事实核验",
  ];
  const accountRefs = [
    "预算分层标题收藏更高",
    "晚间20:30发布曝光更集中",
    "结论先行结构停留时长更好",
    "踩坑复盘题材评论更活跃",
    "数字清单型封面点击率更高",
    "问答体标题在新人群中转发更高",
    "案例前后对比结构评论更集中",
    "末尾行动建议能提升收藏率",
  ];

  const makeRef = (id: string, title: string, source: string, scopedToAccount: boolean, idx: number) => ({
    id,
    account_id: scopedToAccount ? accountId : null,
    source_type: source,
    source_url: null,
    captured_at: isoOffset(now, -(idx * 15 + 20)),
    raw_text: `${title}（演示数据）`,
    meta_jsonb: { title },
  });

  const overviewGlobal: OpsOverview = {
    status: "ok",
    account_id: null,
    domain: { id: domainId, slug: "japan_immigration", name: "日本移民内容增长实验室" },
    channel_stats: {
      xiaohongshu: {
        total: 11,
        pending_review: 3,
        published: 6,
        done: 1,
        failed: 1,
        published_7d: 6,
      },
    },
    reference_items: [
      ...publicRefs.map((title, idx) => makeRef(`pub-${idx + 1}`, title, "demo_public_hotspot", false, idx)),
      ...accountRefs.map((title, idx) => makeRef(`acc-${idx + 1}`, title, "demo_account_signal", true, idx)),
    ],
    latest_tasks: [],
    latest_reports: [],
    analysis_logic: ["采集热点", "结合账号定位", "生成草稿", "人工审核", "复盘回写SOP"],
    strategy_snapshot: {
      focus_keywords: ["日本移民", "经营管理签证", "日本永住"],
      hotspot_queries: ["日本移民", "日本经营管理签证", "日本高度人才签证"],
      automation_policy: { mode: "manual_review" },
    },
  };

  const overviewAccount: OpsOverview = {
    ...overviewGlobal,
    account_id: accountId,
    reference_items: accountRefs.map((title, idx) => makeRef(`acc-only-${idx + 1}`, title, "demo_account_signal", true, idx)),
  };

  const pendingTasks: PipelineTask[] = [
    {
      id: "demo-pending-1",
      domain_id: domainId,
      account_id: accountId,
      channel: "xiaohongshu",
      content_type: "post",
      status: "pending_review",
      stage: "pending_review",
      intent_jsonb: { topic: "经营管理签证：先整理哪些准备问题" },
      payload_jsonb: {
        title: "经营管理签证：先整理哪些准备问题",
        body: "【示例草稿，仅用于流程演示】\n\n准备做一篇经营管理签证主题内容时，先把读者的问题整理出来：关心什么材料、如何判断信息是否适用、应该向谁核实。\n\n正文结构：读者问题 → 待核实事实 → 来源与适用条件 → 后续核对清单。发布前由人工检查每一条事实，补上有效来源；不根据预置草稿给出金额或办理结论。\n\n审核检查：标题是否准确？正文是否有依据？有没有夸大承诺？这份示例仅用于修改、保存和审核状态展示。",
        full_body: "【示例草稿，仅用于流程演示】\n\n准备做一篇经营管理签证主题内容时，先把读者的问题整理出来：关心什么材料、如何判断信息是否适用、应该向谁核实。\n\n正文结构：读者问题 → 待核实事实 → 来源与适用条件 → 后续核对清单。发布前由人工检查每一条事实，补上有效来源；不根据预置草稿给出金额或办理结论。\n\n审核检查：标题是否准确？正文是否有依据？有没有夸大承诺？这份示例仅用于修改、保存和审核状态展示。",
        tags: ["#日本移民", "#经营管理签证", "#日本永住"],
        topic: "经营管理签证：先整理哪些准备问题",
      },
      review_jsonb: {},
      publish_jsonb: {},
      metrics_jsonb: {},
      scheduled_at: isoOffset(now, 60),
      published_at: null,
      created_by: "demo",
      created_at: isoOffset(now, -220),
      updated_at: isoOffset(now, -35),
    },
    {
      id: "demo-pending-2",
      domain_id: domainId,
      account_id: accountId,
      channel: "xiaohongshu",
      content_type: "post",
      status: "pending_review",
      stage: "pending_review",
      intent_jsonb: { topic: "日本工签与永住：读者问题清单" },
      payload_jsonb: {
        title: "日本工签与永住：读者问题清单",
        body: "【示例草稿，仅用于流程演示】\n\n选题：工签与永住相关内容，怎样回应读者的常见疑问？\n\n先列出三个待核对问题：不同身份的适用条件、需要查阅的原始资料、信息更新时间。文章中区分已核实事实与尚待确认内容；缺少来源的段落先不发布。\n\n审核检查：条件是否写全？来源是否对应？是否把个例当成通用规则？此示例不提供实际办理建议。",
        full_body: "【示例草稿，仅用于流程演示】\n\n选题：工签与永住相关内容，怎样回应读者的常见疑问？\n\n先列出三个待核对问题：不同身份的适用条件、需要查阅的原始资料、信息更新时间。文章中区分已核实事实与尚待确认内容；缺少来源的段落先不发布。\n\n审核检查：条件是否写全？来源是否对应？是否把个例当成通用规则？此示例不提供实际办理建议。",
        tags: ["#日本移民", "#日本工签", "#日本永住"],
        topic: "日本工签与永住：读者问题清单",
      },
      review_jsonb: {},
      publish_jsonb: {},
      metrics_jsonb: {},
      scheduled_at: isoOffset(now, 120),
      published_at: null,
      created_by: "demo",
      created_at: isoOffset(now, -190),
      updated_at: isoOffset(now, -28),
    },
    {
      id: "demo-pending-3",
      domain_id: domainId,
      account_id: accountId,
      channel: "xiaohongshu",
      content_type: "post",
      status: "pending_review",
      stage: "pending_review",
      intent_jsonb: { topic: "赴日规划：先明确家庭目标" },
      payload_jsonb: {
        title: "赴日规划：先明确家庭目标",
        body: "【示例草稿，仅用于流程演示】\n\n选题：规划赴日内容时，先明确家庭读者关心的目标。\n\n可按教育、工作与生活安排组织问题，再整理需要核实的信息。正文展示问题框架和查证方向，不替读者作决定，也不承诺实际结果。\n\n审核检查：目标人群是否明确？信息和建议是否区分？行动提示能否帮助读者进一步查证？",
        full_body: "【示例草稿，仅用于流程演示】\n\n选题：规划赴日内容时，先明确家庭读者关心的目标。\n\n可按教育、工作与生活安排组织问题，再整理需要核实的信息。正文展示问题框架和查证方向，不替读者作决定，也不承诺实际结果。\n\n审核检查：目标人群是否明确？信息和建议是否区分？行动提示能否帮助读者进一步查证？",
        tags: ["#日本移民", "#经营管理签证", "#日本永住"],
        topic: "赴日规划：先明确家庭目标",
      },
      review_jsonb: {},
      publish_jsonb: {},
      metrics_jsonb: {},
      scheduled_at: isoOffset(now, 180),
      published_at: null,
      created_by: "demo",
      created_at: isoOffset(now, -160),
      updated_at: isoOffset(now, -20),
    },
  ];

  const publishSummary: PublishFeedbackSummaryItem[] = [
    {
      task_id: "demo-published-1",
      account_id: accountId,
      account_name: accountName,
      title: "示例已发布内容：材料准备问题清单",
      status: "published",
      stage: "published",
      feedback_state: "collecting",
      metrics_mode: "manual_import",
      next_feedback_at: isoOffset(now, 90),
      last_feedback_at: isoOffset(now, -120),
      ces_score: 0.76,
      primary_bottleneck: "hook_strength",
      ee_mode: "stable",
      published_url: "",
      feed_id: "demo_feed_1",
      published_at: isoOffset(now, -1440),
      updated_at: isoOffset(now, -15),
      metrics_current: { impressions: 18234, likes: 663, collects: 412, comments_count: 156, shares: 81 },
      metric_change: { impressions: 0.12, likes: 0.08 },
      snapshot_count: 2,
      historical_imported: true,
      schedule_hours: [1, 3, 24],
      completed_hours: [1],
      checkpoints: [
        { hour: 1, due_at: isoOffset(now, -30), status: "done", label: "发布+1h" },
        { hour: 3, due_at: isoOffset(now, 90), status: "waiting", label: "发布+3h" },
        { hour: 24, due_at: isoOffset(now, 1200), status: "waiting", label: "发布+24h" },
      ],
    },
  ];

  const pendingStrategy: PendingStrategyItem[] = [
    {
      id: "demo-memory-1",
      account_id: accountId,
      effective_account_id: accountId,
      account_name: accountName,
      type: "strategy_rule",
      title: "发布时间调整建议",
      content: "晚间20:30曝光更稳，建议固定默认发布时间。",
      source: "daily_ops",
      suggested_action: "confirm_apply_change",
      affected_agent: "rebuild_strategy",
      confidence: 0.84,
      tags: ["daily_ops", "rebuild_strategy"],
      linked_prompt_version_candidates: [],
      proposed_changes: [
        {
          target: "publish_preferences",
          label: "发布时间/节奏",
          reason: "近7日晚间窗口表现更稳",
          why: "将节奏固化便于复用",
          before_summary: "next_publish_slot_local=19:30",
          after_summary: "next_publish_slot_local=20:30",
          before: { next_publish_slot_local: "19:30" },
          after: { next_publish_slot_local: "20:30" },
          effect: "次日调度默认晚间发布",
        },
      ],
      created_at: isoOffset(now, -80),
      updated_at: isoOffset(now, -55),
    },
    {
      id: "demo-memory-2",
      account_id: accountId,
      effective_account_id: accountId,
      account_name: accountName,
      type: "asset",
      title: "正文开头模板建议",
      content: "开头2句必须先给结论+预算边界。",
      source: "chief_evolution",
      suggested_action: "generate_prompt_version",
      affected_agent: "draft_writer",
      confidence: 0.82,
      tags: ["chief_evolution", "draft_writer"],
      linked_prompt_version_candidates: [],
      proposed_changes: [],
      created_at: isoOffset(now, -70),
      updated_at: isoOffset(now, -48),
    },
    {
      id: "demo-memory-3",
      account_id: accountId,
      effective_account_id: accountId,
      account_name: accountName,
      type: "strategy_rule",
      title: "标题长度与信息密度优化",
      content: "标题控制在18-22字，必须包含一个明确结果词（能/不能/先做什么）。",
      source: "review_agent",
      suggested_action: "confirm_apply_change",
      affected_agent: "draft_writer",
      confidence: 0.79,
      tags: ["review_agent", "title_rule"],
      linked_prompt_version_candidates: [],
      proposed_changes: [],
      created_at: isoOffset(now, -62),
      updated_at: isoOffset(now, -44),
    },
    {
      id: "demo-memory-4",
      account_id: accountId,
      effective_account_id: accountId,
      account_name: accountName,
      type: "asset",
      title: "评论区引导句模板",
      content: "收尾补一句“你现在卡在哪一步？我按你的预算给你建议”，提升互动率。",
      source: "growth_retro",
      suggested_action: "generate_prompt_version",
      affected_agent: "copy_agent",
      confidence: 0.77,
      tags: ["growth_retro", "engagement"],
      linked_prompt_version_candidates: [],
      proposed_changes: [],
      created_at: isoOffset(now, -56),
      updated_at: isoOffset(now, -36),
    },
  ];

  const coachActions: CoachAction[] = [
    {
      id: "demo-coach-action-1",
      title: "补齐今日采集输入",
      description: "先采集一轮热点与观点输入，确保今天选题有新证据。",
      reason: "最近24小时输入不足会导致选题滞后。",
      acceptance: "新增至少8条有效线索",
      risk: "登录失效会影响采集",
      evidence_tags: ["KB+FB"],
      evidence_refs: [{ source_type: "intelligence_items", source_ref: "demo_ref_1", timestamp: isoOffset(now, -40) }],
      requires_confirmation: true,
      execution_supported: true,
      execution: { action_type: "collect_intel" },
      state: "suggested",
    },
    {
      id: "demo-coach-action-2",
      title: "刷新今日选题池",
      description: "按热点+账号定位重排5条优先选题。",
      reason: "候选选题积压，需要先排序。",
      acceptance: "产出5条带优先级选题",
      risk: "证据不足会偏题",
      evidence_tags: ["KB+FB"],
      evidence_refs: [{ source_type: "topics", source_ref: "demo_topic_1", timestamp: isoOffset(now, -50) }],
      requires_confirmation: true,
      execution_supported: false,
      execution: { action_type: "manual_topic_planning" },
      state: "suggested",
    },
    {
      id: "demo-coach-action-3",
      title: "推进待审核内容",
      description: "先处理3条待审核任务，清空积压。",
      reason: "积压会拖慢反馈闭环。",
      acceptance: "待审核至少减少2条",
      risk: "审核标准不一致",
      evidence_tags: ["FB"],
      evidence_refs: [{ source_type: "pipeline_tasks", source_ref: "demo-pending-1", timestamp: isoOffset(now, -30) }],
      requires_confirmation: true,
      execution_supported: false,
      execution: { action_type: "manual_review_gate" },
      state: "suggested",
    },
    {
      id: "demo-coach-action-4",
      title: "回收发布反馈并复盘",
      description: "拉取近24h反馈，生成可执行改进建议。",
      reason: "没有复盘无法稳定升级SOP。",
      acceptance: "输出1条明确改进行动",
      risk: "样本不足时结论不稳定",
      evidence_tags: ["FB"],
      evidence_refs: [{ source_type: "reviews", source_ref: "demo-review-1", timestamp: isoOffset(now, -20) }],
      requires_confirmation: true,
      execution_supported: true,
      execution: { action_type: "reconcile_feedback" },
      state: "suggested",
    },
    {
      id: "demo-coach-action-5",
      title: "校准提示词版本",
      description: "根据昨日表现微调文案提示词。",
      reason: "策略不更新会导致质量停滞。",
      acceptance: "形成1条提示词升级提案",
      risk: "过度改写可能影响稳定性",
      evidence_tags: ["KB+FB"],
      evidence_refs: [{ source_type: "prompt_versions", source_ref: "demo-v1", timestamp: isoOffset(now, -15) }],
      requires_confirmation: true,
      execution_supported: false,
      execution: { action_type: "manual_prompt_upgrade" },
      state: "suggested",
    },
  ];

  const coachBrief: CoachBrief = {
    status: "ok",
    domain_slug: "japan_immigration",
    account_id: accountId,
    account_name: accountName,
    window_days: 14,
    yesterday_summary: {
      date_local: "2026-04-02",
      impressions: 45566,
      interactions: 2301,
      engagement_rate: 0.0505,
      review_pass_rate: 0.75,
      approved_count: 3,
      rejected_count: 1,
      top_fail_reasons: [{ reason: "标题过长，首句信息密度不足", count: 1 }],
    },
    today_actions: coachActions,
    decision_gates: [
      { gate_type: "topic", message: "选题确认门待通过", status: "pending" },
      { gate_type: "review", message: "审核决策门待通过", status: "pending" },
      { gate_type: "retro", message: "复盘确认门待通过", status: "pending" },
    ],
    prompt_upgrade_suggestions: [
      {
        title: "强化开头结论密度",
        proposal: "首句必须给结论和预算边界，第二句给适用人群。",
        reason: "高曝光样本均采用该结构。",
        evidence_tags: ["KB+FB"],
      },
    ],
    context_snapshot: {
      counts: { cases: 12, assets: 18, reviews: 6, topics: 9, memory_items: 11 },
      intel_count: 14,
      pipeline_count: 12,
      kb_count: 45,
    },
  };

  return {
    runtime,
    overviewGlobal,
    overviewAccount,
    publishSummary,
    pendingStrategy,
    pendingTasks,
    coachBrief,
    coachActions,
  };
}
