"""
Local graph store for Zep-free MiroFish runs.

Graphs are stored as JSON under backend/uploads/local_graphs. The shape mirrors
the Zep-derived dictionaries used by the API, report tools, and simulation setup.
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..config import Config
from ..utils.llm_client import LLMClient


class LocalGraphStore:
    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = base_dir or os.path.join(os.path.dirname(__file__), "../uploads/local_graphs")
        os.makedirs(self.base_dir, exist_ok=True)

    def _path(self, graph_id: str) -> str:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", graph_id)
        return os.path.join(self.base_dir, f"{safe_id}.json")

    def create_graph(self, name: str, ontology: Optional[Dict[str, Any]] = None) -> str:
        graph_id = f"local_{uuid.uuid4().hex[:16]}"
        now = datetime.now(timezone.utc).isoformat()
        self.save_graph({
            "graph_id": graph_id,
            "name": name,
            "description": "MiroFish local graph",
            "ontology": ontology or {},
            "nodes": [],
            "edges": [],
            "episodes": [],
            "created_at": now,
            "updated_at": now,
        })
        return graph_id

    def save_graph(self, graph: Dict[str, Any]) -> None:
        graph["updated_at"] = datetime.now(timezone.utc).isoformat()
        with open(self._path(graph["graph_id"]), "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False, indent=2)

    def load_graph(self, graph_id: str) -> Dict[str, Any]:
        path = self._path(graph_id)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Local graph not found: {graph_id}")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def delete_graph(self, graph_id: str) -> None:
        path = self._path(graph_id)
        if os.path.exists(path):
            os.remove(path)

    def graph_data(self, graph_id: str) -> Dict[str, Any]:
        graph = self.load_graph(graph_id)
        nodes = graph.get("nodes", [])
        edges = graph.get("edges", [])
        node_map = {n.get("uuid"): n.get("name", "") for n in nodes}
        for edge in edges:
            edge.setdefault("source_node_name", node_map.get(edge.get("source_node_uuid"), ""))
            edge.setdefault("target_node_name", node_map.get(edge.get("target_node_uuid"), ""))
            edge.setdefault("fact_type", edge.get("name", "RELATED_TO"))
            edge.setdefault("attributes", {})
            edge.setdefault("episodes", [])
        return {
            "graph_id": graph_id,
            "nodes": nodes,
            "edges": edges,
            "node_count": len(nodes),
            "edge_count": len(edges),
        }

    def add_activity(self, graph_id: str, activity: Dict[str, Any]) -> None:
        graph = self.load_graph(graph_id)
        graph.setdefault("activities", []).append({
            "uuid": f"activity_{uuid.uuid4().hex[:12]}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "data": activity,
        })
        self.save_graph(graph)


class LocalGraphExtractor:
    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.llm = llm_client or LLMClient()

    def extract(self, chunks: List[str], ontology: Dict[str, Any], progress_callback=None) -> Dict[str, List[Dict[str, Any]]]:
        nodes_by_key: Dict[str, Dict[str, Any]] = {}
        edges_by_key: Dict[str, Dict[str, Any]] = {}
        total = max(len(chunks), 1)

        for index, chunk in enumerate(chunks, 1):
            if progress_callback:
                progress_callback(f"Local extraction with LM Studio: chunk {index}/{total}", index / total)
            extracted = self._extract_chunk(chunk, ontology)
            for node in extracted.get("nodes", []):
                name = str(node.get("name", "")).strip()
                if not name:
                    continue
                labels = node.get("labels") or node.get("types") or ["Entity"]
                labels = [str(label) for label in labels if str(label).strip()]
                if "Entity" not in labels:
                    labels.insert(0, "Entity")
                key = name.lower()
                existing = nodes_by_key.get(key)
                if existing:
                    existing["labels"] = sorted(set(existing.get("labels", []) + labels))
                    summary = str(node.get("summary", "")).strip()
                    if summary and summary not in existing.get("summary", ""):
                        existing["summary"] = (existing.get("summary", "") + "\n" + summary).strip()
                    existing.setdefault("attributes", {}).update(node.get("attributes") or {})
                else:
                    nodes_by_key[key] = {
                        "uuid": f"node_{uuid.uuid4().hex[:16]}",
                        "name": name,
                        "labels": labels,
                        "summary": str(node.get("summary", "")).strip(),
                        "attributes": node.get("attributes") or {},
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }

            for edge in extracted.get("edges", []):
                source = str(edge.get("source") or edge.get("source_name") or "").strip()
                target = str(edge.get("target") or edge.get("target_name") or "").strip()
                if not source or not target:
                    continue
                source_node = nodes_by_key.get(source.lower())
                target_node = nodes_by_key.get(target.lower())
                if not source_node or not target_node:
                    continue
                name = str(edge.get("name") or edge.get("relation") or "RELATED_TO").strip() or "RELATED_TO"
                fact = str(edge.get("fact") or f"{source} {name} {target}").strip()
                key = f"{source.lower()}|{name.lower()}|{target.lower()}|{fact.lower()}"
                if key not in edges_by_key:
                    edges_by_key[key] = {
                        "uuid": f"edge_{uuid.uuid4().hex[:16]}",
                        "name": name,
                        "fact": fact,
                        "fact_type": name,
                        "source_node_uuid": source_node["uuid"],
                        "target_node_uuid": target_node["uuid"],
                        "source_node_name": source_node["name"],
                        "target_node_name": target_node["name"],
                        "attributes": edge.get("attributes") or {},
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "valid_at": None,
                        "invalid_at": None,
                        "expired_at": None,
                        "episodes": [],
                    }

        return {"nodes": list(nodes_by_key.values()), "edges": list(edges_by_key.values())}

    def _extract_chunk(self, chunk: str, ontology: Dict[str, Any]) -> Dict[str, Any]:
        entity_types = [e.get("name") for e in ontology.get("entity_types", []) if e.get("name")]
        edge_types = [e.get("name") for e in ontology.get("edge_types", []) if e.get("name")]
        prompt = (
            "Extract a compact knowledge graph from the text. Return JSON only with keys "
            "nodes and edges. Node fields: name, labels, summary, attributes. Edge fields: "
            "source, target, name, fact, attributes. Use these entity types when appropriate: "
            f"{entity_types}. Use these relationship types when appropriate: {edge_types}.\n\n"
            f"Text:\n{chunk[:12000]}"
        )
        data = self.llm.chat_json(
            messages=[
                {"role": "system", "content": "You extract factual graph JSON for a local simulation app."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=4096,
        )
        if not isinstance(data, dict):
            return {"nodes": [], "edges": []}
        return {
            "nodes": data.get("nodes") if isinstance(data.get("nodes"), list) else [],
            "edges": data.get("edges") if isinstance(data.get("edges"), list) else [],
        }
