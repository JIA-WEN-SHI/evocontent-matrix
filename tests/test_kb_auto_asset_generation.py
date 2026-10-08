from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "services" / "api"))

from app.services.kb_service import _build_asset_candidates_from_case  # noqa: E402


def test_build_asset_candidates_from_case_returns_four_templates():
    candidates = _build_asset_candidates_from_case(
        {
            "title": "日本经营管理签证申请避坑",
            "content": "先看条件。再看成本。最后看时间线。",
            "metrics": {"likes": 20, "collects": 10, "comments_count": 5},
        }
    )
    assert len(candidates) == 4
    asset_types = {item["type"] for item in candidates}
    assert asset_types == {"method_card", "topic_angle", "opening_template", "title_template"}

