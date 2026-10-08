from functools import partial

from langgraph.graph import END, START, StateGraph
from supabase import Client

from app.nodes.workflow_nodes import (
    collect_metrics,
    create_assets_prompt,
    draft_writer,
    fetch_recent_intel,
    human_review_waiter,
    load_domain_strategy,
    publisher_playwright,
    reflect_and_upgrade_strategy,
    route_by_status,
)
from app.types import GraphState


def build_graph(client: Client):
    graph = StateGraph(GraphState)

    graph.add_node("load_domain_strategy", partial(load_domain_strategy, client=client))
    graph.add_node("fetch_recent_intel", partial(fetch_recent_intel, client=client))
    graph.add_node("draft_writer", draft_writer)
    graph.add_node("create_assets_prompt", create_assets_prompt)
    graph.add_node("human_review_waiter", partial(human_review_waiter, client=client))
    graph.add_node("publisher_playwright", partial(publisher_playwright, client=client))
    graph.add_node("collect_metrics", partial(collect_metrics, client=client))
    graph.add_node("reflect_and_upgrade_strategy", partial(reflect_and_upgrade_strategy, client=client))

    graph.add_edge(START, "load_domain_strategy")
    graph.add_conditional_edges(
        "load_domain_strategy",
        route_by_status,
        {
            "draft_path": "fetch_recent_intel",
            "publish_path": "publisher_playwright",
            "already_pending": END,
            "stop": END,
        },
    )
    graph.add_edge("fetch_recent_intel", "draft_writer")
    graph.add_edge("draft_writer", "create_assets_prompt")
    graph.add_edge("create_assets_prompt", "human_review_waiter")
    graph.add_edge("human_review_waiter", END)

    graph.add_edge("publisher_playwright", "collect_metrics")
    graph.add_edge("collect_metrics", "reflect_and_upgrade_strategy")
    graph.add_edge("reflect_and_upgrade_strategy", END)

    return graph.compile()

