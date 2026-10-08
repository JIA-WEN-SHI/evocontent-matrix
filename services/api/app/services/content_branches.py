"""Validate the account writing branch and snapshot its supplied source."""
from urllib.parse import urlsplit

from fastapi import HTTPException

BRANCHES = {"tutorial", "project_observer"}


def branch_selection(overrides):
    branch = overrides.get("content_branch", "tutorial")
    if branch not in BRANCHES:
        raise HTTPException(422, "未知内容分支，请选择实用教程或 AI 项目观察·共情短文")
    return branch


def project_reference_ready(ref):
    if not isinstance(ref, dict):
        return False
    for key in ("name", "source_url", "summary", "advantages", "input", "output", "checked_at"):
        if not isinstance(ref.get(key), str) or not ref[key].strip() or len(ref[key]) > 3000:
            return False
    try:
        url = urlsplit(ref["source_url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            return False
    except ValueError:
        return False
    keywords = ref.get("technology_keywords")
    return isinstance(keywords, list) and 0 < len(keywords) <= 12 and all(
        isinstance(word, str) and 0 < len(word.strip()) <= 80 for word in keywords)
