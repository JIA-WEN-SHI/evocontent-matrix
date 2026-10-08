"""Account-selected writing contracts shared by drafting and rewriting."""
import re
from urllib.parse import urlsplit

PROJECT_OBSERVER = "project_observer"
BRANCH_NAME = "AI 项目观察·共情短文"
BRANCH_VERSION = "1.1"
OBSERVER_RULES = """内容分支：AI 项目观察·共情短文（v1.1）。
标题首先传达 AI + 行业：明确写出 AI 和 project_reference.industry 中的具体行业，可再点出一个有来源支持的能力。
标题不是职业反应或情绪画面的摘要，不提前说“画笔还握在手里”等结尾动作；不写整个行业消失等夸大判断。
核心目的是共情：项目是引子，让读者联想到自己投入的时间、练过的本事、赖以生活的工作。
视角是清醒的第三者，平静陈述，不代替读者解释情绪，不渲染整个行业已经消失。
信息结构不是固定句式：技术手段 -> 用途和具体优势 -> 输入与输出 -> 一个职业生活画面 -> 最后揭晓项目名称。
可以从见闻、一个输入示例或演示能力切入，允许调整前几项顺序，不要每篇以“今天看到”开头。
正文约 150-300 字，最多 420 字；短段落，简介用易懂的话带出技术关键词，不写长教程。
项目事实只来自 project_reference 和提供的来源，不捏造速度、费用、普适效果、测试经历或行业人士反应。
明确输入什么、输出什么，可以在句子里写输入/输出，不必固定两行；末行仅揭晓项目名称。
职业画面必须是简短的设想，用“想象”“假如”或“如果”明示，不冒充目击或真实采访。
画面贴合具体职业，不重复套用“学了十几年”“他没说话，又看了一遍”，不说他们落寞、无奈或该失业。
禁用“我觉得”“我认为”“至少”“但是”“不过”“先说结论”；不用拉扯、空泛趋势判断或卖课腔。
不补“做出初版，正在变得容易”“生成结果需要检查修改”等模板句；真实必要限制留在来源说明供审核。
不写“更流畅便捷”“满足多样化需求”“大大节省时间”等营销结论，不把职业画面改写成教人如何修改草稿。
介绍最多两三句；职业画面仅一两句动作，不解释意义，不建议转型或继续学习。
不追加评论提问、私信引导、标签、免责声明段落；项目名称必须是正文最后一句。
保留来源链接，不在正文提前揭晓名称；链接可用“项目介绍”作为文字。来源说明和审核记录不计作正文。
生成与改写都遵守本分支；原有教程结构、线索转化钩子和互动规则不适用于此分支。
"""


def resolve_content_branch(intent, account_strategy):
    overrides = account_strategy.get("prompt_overrides") or {}
    value = intent.get("content_branch", overrides.get("content_branch", "tutorial"))
    if value not in {"tutorial", PROJECT_OBSERVER}:
        raise ValueError("未知内容分支，请选择实用教程或 AI 项目观察·共情短文")
    return value


def project_source_ready(project):
    if not isinstance(project, dict):
        return False
    required = ("name", "source_url", "summary", "advantages", "input", "output", "checked_at")
    if not all(isinstance(project.get(key), str) and project[key].strip() for key in required):
        return False
    try:
        url = urlsplit(project["source_url"])
        valid_url = url.scheme == "https" and bool(url.hostname) and not url.username and not url.password
    except ValueError:
        return False
    keywords = project.get("technology_keywords")
    return valid_url and isinstance(keywords, list) and 0 < len(keywords) <= 12 and all(
        isinstance(word, str) and 0 < len(word.strip()) <= 80 for word in keywords)


def observer_strategy(strategy, project, variant="discovery_first"):
    result = dict(strategy)
    result.update({
        "content_branch": PROJECT_OBSERVER, "project_reference": project if isinstance(project, dict) else {},
        "content_variant": variant,
        "brand_tone": "平静、具体、第三者陈述，情绪留在职业生活画面里",
        "hooks": [], "focus_keywords": (project or {}).get("technology_keywords", []),
        "persona_guardrail": "只写 AI 项目观察；以读者共情为目标，事实和设想必须分开，禁止虚构亲测。",
        "draft": OBSERVER_RULES,
    })
    return result


def observer_fallback(strategy):
    project = strategy.get("project_reference") or {}
    if not project_source_ready(project):
        return {"title": "项目资料待补齐，暂不生成正文", "body": ""}
    summary = str(project["summary"]).strip().rstrip("。")
    advantages = str(project["advantages"]).strip().rstrip("。")
    intro = f"它{summary.removeprefix('它')}。{advantages}。[项目介绍]({project['source_url']})"
    io = f"输入{project['input'].rstrip('。')}。\n输出{project['output'].rstrip('。')}。"
    if strategy.get("content_variant") == "input_first":
        parts = [io, intro]
    elif strategy.get("content_variant") == "capability_first":
        parts = [intro, io]
    else:
        parts = ["今天看到一个开源项目。", intro, io]
    # No fictional reaction is invented when a source has no occupational context.
    role = str(project.get("affected_role") or "").strip()
    scene = str(project.get("imagined_scene") or "").strip()
    if role and scene:
        parts.extend([f"想象一个{role}看到这个演示。", scene])
    parts.append(f"这个项目叫 {project['name']}。")
    industry = str(project.get("industry") or "").strip()
    headline = str(project.get("headline") or "").strip()
    if not industry or industry not in headline or not re.search(r"(?<![A-Za-z])AI(?![A-Za-z])", headline, re.I):
        headline = f"AI + {industry}：项目能力观察" if industry else "所属行业待补齐"
    return {"title": headline, "body": "\n\n".join(parts)}


def observer_contract_checks(body, project, title=""):
    project = project if isinstance(project, dict) else {}
    plain = re.sub(r"\[([^\]]+)\]\([^\s)]+\)", r"\1", body)
    lines = [line.strip() for line in plain.splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    name = str(project.get("name") or "").strip()
    name_last = bool(name and re.search(r"(?:项目叫|叫作|叫|名为)\s*" + re.escape(name) + r"[。.!！]*$", last))
    name_last = name_last and name not in "\n".join(lines[:-1]) and name not in title
    unwanted = ("我觉得", "我认为", "至少", "但是", "不过", "先说结论", "评论区", "欢迎留言", "私信",
                "做出初版，正在变得容易", "生成结果需要检查", "需要检查和修改", "行业失去了意义",
                "流畅便捷", "满足多样化需求", "大大节省", "视觉草稿")
    style_hits = [token for token in unwanted if token in plain]
    input_output = "输入" in plain and "输出" in plain
    technology = project.get("technology_keywords") or []
    industry = str(project.get("industry") or "").strip()
    return [
        {"name": "title_industry", "passed": bool(industry and industry in title and re.search(r"(?<![A-Za-z])AI(?![A-Za-z])", title, re.I)),
         "weight": 10, "detail": "标题必须明确 AI 与所属行业"},
        {"name": "project_source", "passed": project_source_ready(project), "weight": 15,
         "detail": project.get("source_url") or "缺少项目名称、来源、核对时间或能力资料"},
        {"name": "source_link", "passed": bool(project.get('source_url') and project['source_url'] in body),
         "weight": 5, "detail": "正文保留完整来源链接"},
        {"name": "project_name_last", "passed": name_last, "weight": 10, "detail": last},
        {"name": "observer_style", "passed": not style_hits and not re.search(r"(?:^|\s)#[^\s#]+", plain),
         "weight": 10, "detail": style_hits},
        {"name": "imagined_reaction", "passed": any(word in plain for word in ("想象", "假如", "如果")),
         "weight": 10, "detail": "职业画面必须明确是设想，不冒充真人经历"},
        {"name": "input_output", "passed": input_output, "weight": 10, "detail": input_output},
        {"name": "technology_keywords", "passed": any(str(word) in plain for word in technology),
         "weight": 5, "detail": technology},
    ]
