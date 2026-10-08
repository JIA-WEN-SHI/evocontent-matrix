from typing import Any, Dict, List, Optional, TypedDict


class GraphState(TypedDict, total=False):
    task_id: str
    task: Dict[str, Any]
    domain: Dict[str, Any]
    strategy: Dict[str, Any]
    intel_items: List[Dict[str, Any]]
    draft_title: str
    draft_body: str
    image_prompt: str
    publish_result: Dict[str, Any]
    reflection_result: Dict[str, Any]
    error: str

