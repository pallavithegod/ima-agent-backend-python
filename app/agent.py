from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .database import create_incident
from .services.llm import llm_service
from .services.memory import memory_service


class IncidentState(TypedDict, total=False):
    input: dict[str, Any]
    classification: dict[str, str]
    memories: list[dict[str, Any]]
    analysis: dict[str, Any]
    incident: dict[str, Any]


def classify_node(state: IncidentState) -> IncidentState:
    item = state["input"]
    classification = llm_service.classify(
        item["title"],
        item["description"],
        item.get("error_logs", ""),
        item.get("service_hint"),
    )
    if item.get("severity_hint"):
        classification["severity"] = item["severity_hint"]
    return {"classification": classification}


def recall_node(state: IncidentState) -> IncidentState:
    query = {**state["input"], **state["classification"]}
    return {"memories": memory_service.recall(query)}


def diagnose_node(state: IncidentState) -> IncidentState:
    current = {**state["input"], **state["classification"]}
    return {"analysis": llm_service.diagnose(current, state["memories"])}


def persist_node(state: IncidentState) -> IncidentState:
    item = state["input"]
    classification = state["classification"]
    analysis = state["analysis"]
    best_score = state["memories"][0]["similarity"] if state["memories"] else 0
    novelty = "recurring" if best_score >= 0.72 else ("related" if best_score >= 0.45 else "new")
    now = datetime.now(UTC).isoformat()
    incident = create_incident(
        {
            **item,
            **classification,
            **analysis,
            "novelty": novelty,
            "status": "open",
            "retrieved_memories": state["memories"],
            "timeline": [
                {"at": now, "event": "Incident reported"},
                {"at": now, "event": "Agent classified and searched incident memory"},
                {"at": now, "event": "Initial diagnosis generated"},
            ],
        }
    )
    memory_service.retain(incident)
    return {"incident": incident}


builder = StateGraph(IncidentState)
builder.add_node("classify", classify_node)
builder.add_node("recall", recall_node)
builder.add_node("diagnose", diagnose_node)
builder.add_node("persist", persist_node)
builder.add_edge(START, "classify")
builder.add_edge("classify", "recall")
builder.add_edge("recall", "diagnose")
builder.add_edge("diagnose", "persist")
builder.add_edge("persist", END)
incident_graph = builder.compile()


def analyze_incident(payload: dict[str, Any]) -> dict[str, Any]:
    result = incident_graph.invoke({"input": payload})
    return result["incident"]

