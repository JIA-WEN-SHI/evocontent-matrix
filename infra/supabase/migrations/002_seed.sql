-- Seed: japan_immigration domain + initial strategy (real business profile)
with inserted_domain as (
  insert into domains (slug, name, config_jsonb)
  values (
    'japan_immigration',
    'Japan Immigration',
    jsonb_build_object(
      'audience', jsonb_build_array(
        '国内35岁以上、有中产焦虑，希望为子女规划教育或进行资产避险的企业高管/大厂员工'
      ),
      'pain_points', jsonb_build_array(
        '国内教育内卷',
        '35岁职业焦虑',
        '资产单一化风险'
      ),
      'hooks', jsonb_build_array(
        '后悔没早知道...',
        '预算XX万，全家拿日本身份',
        '为了孩子不卷，我做了这个决定'
      ),
      'forbidden_claims', jsonb_build_array(
        '100%下签',
        '绝对包过',
        '过度炫富'
      ),
      'brand_tone', '专业克制但带有情绪共鸣',
      'focus_keywords', jsonb_build_array(
        '日本', '签证', '移民', '永住', '留学', '经营管理', '在留', '归化'
      ),
      'hotspot_queries', jsonb_build_array(
        '日本移民',
        '日本经营管理签证',
        '日本永住',
        '日本留学'
      ),
      'viewpoint_queries', jsonb_build_array(
        '日本移民 观点',
        '日本经营管理签证 劝退',
        '日本移民 避坑',
        '日本留学 真实经历'
      ),
      'channel_rules', jsonb_build_object(
        'xiaohongshu', '短句+强节奏，避免夸张承诺，结尾引导私信领取资料',
        'wechat_mp', '结构化长文，信息完整，加入政策依据与风险提示'
      ),
      'prompt_templates', jsonb_build_object(
        'draft',
        '请采用PAS结构输出内容：P(痛点)-A(加剧)-S(解决方案)。语气要求专业克制且有同理心，结尾必须引导用户私信领取《日本经营管理签证评估清单》。',
        'reflection',
        '比较高线索与低线索内容，提炼可执行优化建议，保持合规表达并强化线索引导。'
      ),
      'reflection_policy', jsonb_build_object(
        'window_days', 3,
        'min_samples', 6
      )
    )
  )
  on conflict (slug) do update
  set config_jsonb = excluded.config_jsonb,
      updated_at = now()
  returning id
),
target_domain as (
  select id from inserted_domain
  union
  select id from domains where slug = 'japan_immigration'
  limit 1
),
new_version as (
  insert into strategy_versions (domain_id, version, prompt_jsonb, reason, created_by)
  select
    td.id,
    coalesce((select max(version) from strategy_versions where domain_id = td.id), 0) + 1,
    jsonb_build_object(
      'draft_system',
      '你是日本移民内容策略助手。严格使用PAS结构（痛点-加剧-解决方案），语气专业克制且有同理心。结尾必须引导私信领取《日本经营管理签证评估清单》，严禁绝对化承诺与炫富表达。',
      'reflection_system',
      '你是增长复盘助手。核心目标是提升有效线索转化率，输出可落地且合规的策略升级建议。'
    ),
    'seed: real japan_immigration strategy',
    'system'
  from target_domain td
  returning id, domain_id
)
update domains d
set active_strategy_version_id = nv.id,
    updated_at = now()
from new_version nv
where d.id = nv.domain_id;

-- Fallback: always point to the latest strategy version for this domain.
update domains d
set active_strategy_version_id = latest.id,
    updated_at = now()
from (
  select sv.id, sv.domain_id
  from strategy_versions sv
  join domains dd on dd.id = sv.domain_id
  where dd.slug = 'japan_immigration'
  order by sv.version desc
  limit 1
) latest
where d.id = latest.domain_id;
