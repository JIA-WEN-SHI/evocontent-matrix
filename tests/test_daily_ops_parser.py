from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "services" / "agent"))

from app.services.daily_ops import _select_viewpoint, parse_google_news_rss


def test_parse_google_news_rss_basic():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>日本移民政策更新</title>
      <link>https://example.com/a</link>
      <pubDate>Fri, 20 Feb 2026 10:00:00 GMT</pubDate>
      <description>政策细节变化</description>
    </item>
    <item>
      <title>日本经营管理签证热点</title>
      <link>https://example.com/b</link>
      <pubDate>Fri, 20 Feb 2026 09:00:00 GMT</pubDate>
      <description>用户关注点</description>
    </item>
  </channel>
</rss>
"""
    rows = parse_google_news_rss(xml, limit=2)
    assert len(rows) == 2
    assert rows[0]["title"] == "日本移民政策更新"
    assert rows[0]["link"] == "https://example.com/a"


def test_select_viewpoint_uses_first_item_text():
    viewpoint = _select_viewpoint(
        [
            {"raw_text": "有人说日本经营管理签证门槛太高 | 经验分享"},
            {"raw_text": "第二条"},
        ]
    )
    assert "门槛太高" in viewpoint
