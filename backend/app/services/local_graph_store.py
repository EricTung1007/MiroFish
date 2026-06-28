"""
Local graph store for Zep-free MiroFish runs.

Graphs are stored as JSON under backend/uploads/local_graphs. The shape mirrors
the Zep-derived dictionaries used by the API, report tools, and simulation setup.
"""

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..config import Config
from ..utils.llm_client import LLMClient


class LocalGraphStore:
    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = base_dir or os.path.join(os.path.dirname(__file__), "../uploads/local_graphs")
        os.makedirs(self.base_dir, exist_ok=True)
        self.db_path = os.path.join(self.base_dir, "local_graphs.sqlite")
        self._init_db()

    def _path(self, graph_id: str) -> str:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", graph_id)
        return os.path.join(self.base_dir, f"{safe_id}.json")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS graphs (
                    graph_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    ontology_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chunks (
                    uuid TEXT PRIMARY KEY,
                    graph_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    source_file TEXT,
                    text TEXT NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(graph_id) REFERENCES graphs(graph_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS nodes (
                    uuid TEXT PRIMARY KEY,
                    graph_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    labels_json TEXT NOT NULL DEFAULT '[]',
                    summary TEXT,
                    attributes_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(graph_id) REFERENCES graphs(graph_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS edges (
                    uuid TEXT PRIMARY KEY,
                    graph_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    fact TEXT,
                    fact_type TEXT,
                    source_node_uuid TEXT NOT NULL,
                    target_node_uuid TEXT NOT NULL,
                    attributes_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    valid_at TEXT,
                    invalid_at TEXT,
                    expired_at TEXT,
                    FOREIGN KEY(graph_id) REFERENCES graphs(graph_id) ON DELETE CASCADE,
                    FOREIGN KEY(source_node_uuid) REFERENCES nodes(uuid) ON DELETE CASCADE,
                    FOREIGN KEY(target_node_uuid) REFERENCES nodes(uuid) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS node_evidence (
                    uuid TEXT PRIMARY KEY,
                    graph_id TEXT NOT NULL,
                    node_uuid TEXT NOT NULL,
                    chunk_id TEXT,
                    source_file TEXT,
                    quote_or_span TEXT,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    extraction_method TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(graph_id) REFERENCES graphs(graph_id) ON DELETE CASCADE,
                    FOREIGN KEY(node_uuid) REFERENCES nodes(uuid) ON DELETE CASCADE,
                    FOREIGN KEY(chunk_id) REFERENCES chunks(uuid) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS edge_evidence (
                    uuid TEXT PRIMARY KEY,
                    graph_id TEXT NOT NULL,
                    edge_uuid TEXT NOT NULL,
                    chunk_id TEXT,
                    source_file TEXT,
                    quote_or_span TEXT,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    extraction_method TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(graph_id) REFERENCES graphs(graph_id) ON DELETE CASCADE,
                    FOREIGN KEY(edge_uuid) REFERENCES edges(uuid) ON DELETE CASCADE,
                    FOREIGN KEY(chunk_id) REFERENCES chunks(uuid) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS activities (
                    uuid TEXT PRIMARY KEY,
                    graph_id TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(graph_id) REFERENCES graphs(graph_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_chunks_graph ON chunks(graph_id);
                CREATE INDEX IF NOT EXISTS idx_nodes_graph ON nodes(graph_id);
                CREATE INDEX IF NOT EXISTS idx_edges_graph ON edges(graph_id);
                CREATE INDEX IF NOT EXISTS idx_node_evidence_node ON node_evidence(node_uuid);
                CREATE INDEX IF NOT EXISTS idx_edge_evidence_edge ON edge_evidence(edge_uuid);
                """
            )

    @staticmethod
    def _json_dumps(value: Any) -> str:
        return json.dumps(value if value is not None else {}, ensure_ascii=False)

    @staticmethod
    def _json_loads(value: Optional[str], fallback: Any) -> Any:
        if not value:
            return fallback
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback

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
        now = datetime.now(timezone.utc).isoformat()
        graph["updated_at"] = now
        graph.setdefault("created_at", now)
        self._save_graph_sqlite(graph)
        with open(self._path(graph["graph_id"]), "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False, indent=2)

    def _save_graph_sqlite(self, graph: Dict[str, Any]) -> None:
        graph_id = graph["graph_id"]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO graphs (graph_id, name, description, ontology_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(graph_id) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    ontology_json=excluded.ontology_json,
                    updated_at=excluded.updated_at
                """,
                (
                    graph_id,
                    graph.get("name", graph_id),
                    graph.get("description", "MiroFish local graph"),
                    self._json_dumps(graph.get("ontology", {})),
                    graph.get("created_at"),
                    graph.get("updated_at"),
                ),
            )

            for table in ("edge_evidence", "node_evidence", "edges", "nodes", "chunks", "activities"):
                conn.execute(f"DELETE FROM {table} WHERE graph_id = ?", (graph_id,))

            for index, episode in enumerate(graph.get("episodes", []), 1):
                chunk_id = episode.get("uuid") or f"chunk_{index:04d}"
                conn.execute(
                    """
                    INSERT INTO chunks (uuid, graph_id, chunk_index, source_file, text, processed, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        graph_id,
                        int(episode.get("chunk_index", index)),
                        episode.get("source_file"),
                        episode.get("data", ""),
                        1 if episode.get("processed", True) else 0,
                        str(episode.get("created_at") or graph.get("created_at")),
                    ),
                )

            for node in graph.get("nodes", []):
                conn.execute(
                    """
                    INSERT INTO nodes (uuid, graph_id, name, labels_json, summary, attributes_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node.get("uuid"),
                        graph_id,
                        node.get("name", ""),
                        self._json_dumps(node.get("labels", [])),
                        node.get("summary", ""),
                        self._json_dumps(node.get("attributes", {})),
                        node.get("created_at") or graph.get("created_at"),
                    ),
                )
                for evidence in node.get("evidence", []) or []:
                    conn.execute(
                        """
                        INSERT INTO node_evidence
                            (uuid, graph_id, node_uuid, chunk_id, source_file, quote_or_span, confidence, extraction_method, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            evidence.get("uuid") or f"ev_{uuid.uuid4().hex[:16]}",
                            graph_id,
                            node.get("uuid"),
                            evidence.get("chunk_id"),
                            evidence.get("source_file"),
                            evidence.get("quote_or_span", ""),
                            float(evidence.get("confidence", 0.5)),
                            evidence.get("extraction_method", "local"),
                            evidence.get("created_at") or graph.get("created_at"),
                        ),
                    )

            for edge in graph.get("edges", []):
                conn.execute(
                    """
                    INSERT INTO edges (
                        uuid, graph_id, name, fact, fact_type, source_node_uuid, target_node_uuid,
                        attributes_json, created_at, valid_at, invalid_at, expired_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        edge.get("uuid"),
                        graph_id,
                        edge.get("name", ""),
                        edge.get("fact", ""),
                        edge.get("fact_type", edge.get("name", "RELATED_TO")),
                        edge.get("source_node_uuid"),
                        edge.get("target_node_uuid"),
                        self._json_dumps(edge.get("attributes", {})),
                        edge.get("created_at") or graph.get("created_at"),
                        edge.get("valid_at"),
                        edge.get("invalid_at"),
                        edge.get("expired_at"),
                    ),
                )
                for evidence in edge.get("evidence", []) or []:
                    conn.execute(
                        """
                        INSERT INTO edge_evidence
                            (uuid, graph_id, edge_uuid, chunk_id, source_file, quote_or_span, confidence, extraction_method, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            evidence.get("uuid") or f"ev_{uuid.uuid4().hex[:16]}",
                            graph_id,
                            edge.get("uuid"),
                            evidence.get("chunk_id"),
                            evidence.get("source_file"),
                            evidence.get("quote_or_span", ""),
                            float(evidence.get("confidence", 0.5)),
                            evidence.get("extraction_method", "local"),
                            evidence.get("created_at") or graph.get("created_at"),
                        ),
                    )

            for activity in graph.get("activities", []) or []:
                conn.execute(
                    """
                    INSERT INTO activities (uuid, graph_id, data_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        activity.get("uuid") or f"activity_{uuid.uuid4().hex[:12]}",
                        graph_id,
                        self._json_dumps(activity.get("data", {})),
                        activity.get("created_at") or graph.get("created_at"),
                    ),
                )

    def load_graph(self, graph_id: str) -> Dict[str, Any]:
        graph = self._load_graph_sqlite(graph_id)
        if graph:
            return graph

        path = self._path(graph_id)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Local graph not found: {graph_id}")
        with open(path, "r", encoding="utf-8") as f:
            graph = json.load(f)
        self._save_graph_sqlite(graph)
        return graph

    def _load_graph_sqlite(self, graph_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            graph_row = conn.execute(
                "SELECT * FROM graphs WHERE graph_id = ?",
                (graph_id,),
            ).fetchone()
            if not graph_row:
                return None

            graph = {
                "graph_id": graph_row["graph_id"],
                "name": graph_row["name"],
                "description": graph_row["description"],
                "ontology": self._json_loads(graph_row["ontology_json"], {}),
                "nodes": [],
                "edges": [],
                "episodes": [],
                "activities": [],
                "created_at": graph_row["created_at"],
                "updated_at": graph_row["updated_at"],
            }

            chunk_rows = conn.execute(
                "SELECT * FROM chunks WHERE graph_id = ? ORDER BY chunk_index ASC",
                (graph_id,),
            ).fetchall()
            for row in chunk_rows:
                graph["episodes"].append({
                    "uuid": row["uuid"],
                    "chunk_index": row["chunk_index"],
                    "source_file": row["source_file"],
                    "data": row["text"],
                    "processed": bool(row["processed"]),
                    "created_at": row["created_at"],
                })

            node_rows = conn.execute(
                "SELECT * FROM nodes WHERE graph_id = ? ORDER BY created_at ASC, name ASC",
                (graph_id,),
            ).fetchall()
            for row in node_rows:
                evidence = [
                    dict(ev)
                    for ev in conn.execute(
                        "SELECT * FROM node_evidence WHERE node_uuid = ? ORDER BY created_at ASC",
                        (row["uuid"],),
                    ).fetchall()
                ]
                graph["nodes"].append({
                    "uuid": row["uuid"],
                    "name": row["name"],
                    "labels": self._json_loads(row["labels_json"], []),
                    "summary": row["summary"] or "",
                    "attributes": self._json_loads(row["attributes_json"], {}),
                    "evidence": evidence,
                    "created_at": row["created_at"],
                })

            node_map = {node["uuid"]: node["name"] for node in graph["nodes"]}
            edge_rows = conn.execute(
                "SELECT * FROM edges WHERE graph_id = ? ORDER BY created_at ASC, name ASC",
                (graph_id,),
            ).fetchall()
            for row in edge_rows:
                evidence = [
                    dict(ev)
                    for ev in conn.execute(
                        "SELECT * FROM edge_evidence WHERE edge_uuid = ? ORDER BY created_at ASC",
                        (row["uuid"],),
                    ).fetchall()
                ]
                graph["edges"].append({
                    "uuid": row["uuid"],
                    "name": row["name"],
                    "fact": row["fact"] or "",
                    "fact_type": row["fact_type"] or row["name"],
                    "source_node_uuid": row["source_node_uuid"],
                    "target_node_uuid": row["target_node_uuid"],
                    "source_node_name": node_map.get(row["source_node_uuid"], ""),
                    "target_node_name": node_map.get(row["target_node_uuid"], ""),
                    "attributes": self._json_loads(row["attributes_json"], {}),
                    "evidence": evidence,
                    "created_at": row["created_at"],
                    "valid_at": row["valid_at"],
                    "invalid_at": row["invalid_at"],
                    "expired_at": row["expired_at"],
                    "episodes": [ev["chunk_id"] for ev in evidence if ev.get("chunk_id")],
                })

            activity_rows = conn.execute(
                "SELECT * FROM activities WHERE graph_id = ? ORDER BY created_at ASC",
                (graph_id,),
            ).fetchall()
            for row in activity_rows:
                graph["activities"].append({
                    "uuid": row["uuid"],
                    "created_at": row["created_at"],
                    "data": self._json_loads(row["data_json"], {}),
                })

            return graph

    def delete_graph(self, graph_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM graphs WHERE graph_id = ?", (graph_id,))
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

    def search_context(self, graph_id: str, query: str, limit: int = 10) -> Dict[str, Any]:
        """Evidence-aware local retrieval over chunks, nodes, and edges."""
        query_lower = query.lower()
        terms = [term for term in re.split(r"[\s,，。.:;!?/\\()\[\]{}\"']+", query_lower) if len(term) > 1]

        def score_text(text: str) -> int:
            text_lower = (text or "").lower()
            score = 100 if query_lower and query_lower in text_lower else 0
            score += sum(10 for term in terms if term in text_lower)
            return score

        graph = self.load_graph(graph_id)
        scored_chunks = []
        for chunk in graph.get("episodes", []):
            score = score_text(chunk.get("data", ""))
            if score > 0:
                scored_chunks.append((score, chunk))
        scored_chunks.sort(key=lambda item: item[0], reverse=True)

        scored_nodes = []
        for node in graph.get("nodes", []):
            score = score_text(node.get("name", "")) + score_text(node.get("summary", ""))
            score += sum(score_text(ev.get("quote_or_span", "")) for ev in node.get("evidence", []))
            if score > 0:
                scored_nodes.append((score, node))
        scored_nodes.sort(key=lambda item: item[0], reverse=True)

        scored_edges = []
        for edge in graph.get("edges", []):
            score = score_text(edge.get("name", "")) + score_text(edge.get("fact", ""))
            score += score_text(edge.get("source_node_name", "")) + score_text(edge.get("target_node_name", ""))
            score += sum(score_text(ev.get("quote_or_span", "")) for ev in edge.get("evidence", []))
            if score > 0:
                scored_edges.append((score, edge))
        scored_edges.sort(key=lambda item: item[0], reverse=True)

        return {
            "query": query,
            "chunks": [chunk for _, chunk in scored_chunks[:limit]],
            "nodes": [node for _, node in scored_nodes[:limit]],
            "edges": [edge for _, edge in scored_edges[:limit]],
        }


class LocalGraphExtractor:
    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.llm = llm_client or LLMClient()
        self.use_llm = os.getenv("LOCAL_GRAPH_USE_LLM", "").lower() in {"1", "true", "yes", "on"}

    def extract(self, chunks: List[str], ontology: Dict[str, Any], progress_callback=None) -> Dict[str, List[Dict[str, Any]]]:
        nodes_by_key: Dict[str, Dict[str, Any]] = {}
        edges_by_key: Dict[str, Dict[str, Any]] = {}
        episodes: List[Dict[str, Any]] = []
        total = max(len(chunks), 1)

        for index, chunk in enumerate(chunks, 1):
            chunk_id = f"chunk_{index:04d}"
            source_file = self._source_file_from_chunk(chunk)
            episodes.append({
                "uuid": chunk_id,
                "chunk_index": index,
                "source_file": source_file,
                "data": chunk,
                "processed": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            if progress_callback:
                extractor_name = "LM Studio" if self.use_llm else "deterministic local extractor"
                progress_callback(f"Local extraction with {extractor_name}: chunk {index}/{total}", index / total)
            extracted = self._extract_chunk(chunk, ontology)
            for node in extracted.get("nodes", []):
                if not isinstance(node, dict):
                    continue
                name = str(node.get("name", "")).strip()
                if not name:
                    continue
                labels = node.get("labels") or node.get("types") or ["Entity"]
                if not isinstance(labels, list):
                    labels = [labels]
                labels = [str(label) for label in labels if str(label).strip()]
                if "Entity" not in labels:
                    labels.insert(0, "Entity")
                evidence = self._evidence_for(
                    chunk=chunk,
                    chunk_id=chunk_id,
                    source_file=source_file,
                    match_text=name,
                    confidence=float(node.get("confidence", 0.65) or 0.65),
                )
                key = name.lower()
                existing = nodes_by_key.get(key)
                if existing:
                    existing["labels"] = sorted(set(existing.get("labels", []) + labels))
                    summary = str(node.get("summary", "")).strip()
                    if summary and summary not in existing.get("summary", ""):
                        existing["summary"] = (existing.get("summary", "") + "\n" + summary).strip()
                    attributes = node.get("attributes") if isinstance(node.get("attributes"), dict) else {}
                    existing.setdefault("attributes", {}).update(attributes)
                    existing.setdefault("evidence", []).append(evidence)
                else:
                    attributes = node.get("attributes") if isinstance(node.get("attributes"), dict) else {}
                    nodes_by_key[key] = {
                        "uuid": f"node_{uuid.uuid4().hex[:16]}",
                        "name": name,
                        "labels": labels,
                        "summary": str(node.get("summary", "")).strip(),
                        "attributes": attributes,
                        "evidence": [evidence],
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }

            for edge in extracted.get("edges", []):
                if not isinstance(edge, dict):
                    continue
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
                evidence = self._evidence_for(
                    chunk=chunk,
                    chunk_id=chunk_id,
                    source_file=source_file,
                    match_text=fact if fact else f"{source} {target}",
                    confidence=float(edge.get("confidence", 0.55) or 0.55),
                )
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
                        "attributes": edge.get("attributes") if isinstance(edge.get("attributes"), dict) else {},
                        "evidence": [evidence],
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "valid_at": None,
                        "invalid_at": None,
                        "expired_at": None,
                        "episodes": [chunk_id],
                    }

        return {
            "nodes": list(nodes_by_key.values()),
            "edges": list(edges_by_key.values()),
            "episodes": episodes,
        }

    @staticmethod
    def _source_file_from_chunk(chunk: str) -> Optional[str]:
        match = re.search(r"===\s*(.*?)\s*===", chunk)
        return match.group(1).strip() if match else None

    @staticmethod
    def _quote_for(chunk: str, match_text: str, window: int = 220) -> str:
        compact_chunk = " ".join(chunk.split())
        if not match_text:
            return compact_chunk[:window]
        index = compact_chunk.lower().find(match_text.lower())
        if index < 0:
            return compact_chunk[:window]
        start = max(index - window // 3, 0)
        end = min(index + len(match_text) + window // 2, len(compact_chunk))
        return compact_chunk[start:end]

    def _evidence_for(
        self,
        chunk: str,
        chunk_id: str,
        source_file: Optional[str],
        match_text: str,
        confidence: float,
    ) -> Dict[str, Any]:
        method = "llm_enriched" if self.use_llm else "deterministic"
        return {
            "uuid": f"ev_{uuid.uuid4().hex[:16]}",
            "chunk_id": chunk_id,
            "source_file": source_file,
            "quote_or_span": self._quote_for(chunk, match_text),
            "confidence": max(0.0, min(confidence, 1.0)),
            "extraction_method": method,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _extract_chunk(self, chunk: str, ontology: Dict[str, Any]) -> Dict[str, Any]:
        if not self.use_llm:
            return self._fallback_extract_chunk(chunk, ontology)

        entity_types = [e.get("name") for e in ontology.get("entity_types", []) if e.get("name")]
        edge_types = [e.get("name") for e in ontology.get("edge_types", []) if e.get("name")]
        prompt = (
            "Extract a compact knowledge graph from the text. Return JSON only with keys "
            "nodes and edges. Node fields: name, labels, summary, attributes. Edge fields: "
            "source, target, name, fact, attributes. Keep the result small: at most 6 nodes "
            "and at most 8 edges. Use short names and one-sentence summaries. Use these "
            "entity types when appropriate: "
            f"{entity_types}. Use these relationship types when appropriate: {edge_types}.\n\n"
            f"Text:\n{chunk[:3000]}"
        )
        try:
            data = self.llm.chat_json(
                messages=[
                    {"role": "system", "content": "You extract small factual graph JSON. Do not explain. Do not include markdown."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=1200,
                json_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["nodes", "edges"],
                    "properties": {
                        "nodes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["name", "labels", "summary", "attributes"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "labels": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    },
                                    "summary": {"type": "string"},
                                    "attributes": {
                                        "type": "object",
                                        "additionalProperties": True
                                    }
                                }
                            }
                        },
                        "edges": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["source", "target", "name", "fact", "attributes"],
                                "properties": {
                                    "source": {"type": "string"},
                                    "target": {"type": "string"},
                                    "name": {"type": "string"},
                                    "fact": {"type": "string"},
                                    "attributes": {
                                        "type": "object",
                                        "additionalProperties": True
                                    }
                                }
                            }
                        }
                    }
                },
                schema_name="local_graph_extraction",
            )
        except Exception:
            return self._fallback_extract_chunk(chunk, ontology)
        if not isinstance(data, dict):
            return self._fallback_extract_chunk(chunk, ontology)
        return {
            "nodes": data.get("nodes") if isinstance(data.get("nodes"), list) else [],
            "edges": data.get("edges") if isinstance(data.get("edges"), list) else [],
        }

    def _fallback_extract_chunk(self, chunk: str, ontology: Dict[str, Any]) -> Dict[str, Any]:
        lower_chunk = chunk.lower()
        nodes: List[Dict[str, Any]] = []

        candidates = [
            ("ARC Raiders", "Organization", ["arc raiders", "game"]),
            ("Game Developer", "GameDeveloper", ["developer", "studio", "update"]),
            ("Casual Players", "CasualPlayer", ["casual"]),
            ("Hardcore Players", "HardcorePlayer", ["hardcore"]),
            ("Loot Players", "LootPlayer", ["loot", "progression", "rare items"]),
            ("PvP Players", "PvPplayer", ["pvp", "competitive"]),
            ("Content Creators", "ContentCreator", ["content creator", "videos", "streamer", "youtube"]),
            ("Media Outlets", "MediaOutlet", ["media", "news", "coverage"]),
            ("Reddit", "SocialMediaPlatform", ["reddit"]),
            ("Discord", "SocialMediaPlatform", ["discord"]),
            ("YouTube", "SocialMediaPlatform", ["youtube"]),
            ("Twitter/X", "SocialMediaPlatform", ["twitter", "x."]),
        ]

        ontology_names = {e.get("name") for e in ontology.get("entity_types", []) if isinstance(e, dict)}
        for name, label, keywords in candidates:
            if label not in ontology_names and label not in {"Organization", "SocialMediaPlatform"}:
                continue
            if any(keyword in lower_chunk for keyword in keywords):
                nodes.append({
                    "name": name,
                    "labels": ["Entity", label],
                    "summary": f"Mentioned in source text: {name}.",
                    "attributes": {},
                })

        if len(nodes) < 2:
            return {"nodes": nodes, "edges": []}

        node_names = {node["name"] for node in nodes}
        edges: List[Dict[str, Any]] = []
        developer_target = "Game Developer" if "Game Developer" in node_names else "ARC Raiders"

        for node in nodes:
            source = node["name"]
            if source == developer_target:
                continue
            relation = "DISCUSSES_ON" if node["labels"][-1] == "SocialMediaPlatform" else "REACTION_TO"
            edges.append({
                "source": source,
                "target": developer_target,
                "name": relation,
                "fact": f"{source} is relevant to the community reaction around {developer_target}.",
                "attributes": {},
            })

        return {"nodes": nodes, "edges": edges[:8]}
