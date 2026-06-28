"""
Zep檢索工具服務
封裝圖譜搜索、節點讀取、邊查詢等工具，供Report Agent使用

核心檢索工具（優化後）：
1. InsightForge（深度洞察檢索）- 最強大的混合檢索，自動生成子問題並多維度檢索
2. PanoramaSearch（廣度搜索）- 獲取全貌，包括過期內容
3. QuickSearch（簡單搜索）- 快速檢索
"""

import time
import json
import os
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

try:
    from zep_cloud.client import Zep
except ImportError:
    Zep = None

from ..config import Config
from ..utils.logger import get_logger
from ..utils.llm_client import LLMClient
from ..utils.locale import get_locale, t
from ..utils.zep_paging import fetch_all_nodes, fetch_all_edges
from .local_graph_store import LocalGraphStore

logger = get_logger('mirofish.zep_tools')


@dataclass
class SearchResult:
    """搜索結果"""
    facts: List[str]
    edges: List[Dict[str, Any]]
    nodes: List[Dict[str, Any]]
    query: str
    total_count: int
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "facts": self.facts,
            "edges": self.edges,
            "nodes": self.nodes,
            "query": self.query,
            "total_count": self.total_count
        }
    
    def to_text(self) -> str:
        """轉換為文本格式，供LLM理解"""
        text_parts = [f"搜索查詢: {self.query}", f"找到 {self.total_count} 條相關信息"]
        
        if self.facts:
            text_parts.append("\n### 相關事實:")
            for i, fact in enumerate(self.facts, 1):
                text_parts.append(f"{i}. {fact}")
        
        return "\n".join(text_parts)


@dataclass
class NodeInfo:
    """節點信息"""
    uuid: str
    name: str
    labels: List[str]
    summary: str
    attributes: Dict[str, Any]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "labels": self.labels,
            "summary": self.summary,
            "attributes": self.attributes
        }
    
    def to_text(self) -> str:
        """轉換為文本格式"""
        entity_type = next((l for l in self.labels if l not in ["Entity", "Node"]), "未知類型")
        return f"實體: {self.name} (類型: {entity_type})\n摘要: {self.summary}"


@dataclass
class EdgeInfo:
    """邊信息"""
    uuid: str
    name: str
    fact: str
    source_node_uuid: str
    target_node_uuid: str
    source_node_name: Optional[str] = None
    target_node_name: Optional[str] = None
    # 時間信息
    created_at: Optional[str] = None
    valid_at: Optional[str] = None
    invalid_at: Optional[str] = None
    expired_at: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "fact": self.fact,
            "source_node_uuid": self.source_node_uuid,
            "target_node_uuid": self.target_node_uuid,
            "source_node_name": self.source_node_name,
            "target_node_name": self.target_node_name,
            "created_at": self.created_at,
            "valid_at": self.valid_at,
            "invalid_at": self.invalid_at,
            "expired_at": self.expired_at
        }
    
    def to_text(self, include_temporal: bool = False) -> str:
        """轉換為文本格式"""
        source = self.source_node_name or self.source_node_uuid[:8]
        target = self.target_node_name or self.target_node_uuid[:8]
        base_text = f"關係: {source} --[{self.name}]--> {target}\n事實: {self.fact}"
        
        if include_temporal:
            valid_at = self.valid_at or "未知"
            invalid_at = self.invalid_at or "至今"
            base_text += f"\n時效: {valid_at} - {invalid_at}"
            if self.expired_at:
                base_text += f" (已過期: {self.expired_at})"
        
        return base_text
    
    @property
    def is_expired(self) -> bool:
        """是否已過期"""
        return self.expired_at is not None
    
    @property
    def is_invalid(self) -> bool:
        """是否已失效"""
        return self.invalid_at is not None


@dataclass
class InsightForgeResult:
    """
    深度洞察檢索結果 (InsightForge)
    包含多個子問題的檢索結果，以及綜合分析
    """
    query: str
    simulation_requirement: str
    sub_queries: List[str]
    
    # 各維度檢索結果
    semantic_facts: List[str] = field(default_factory=list)  # 語義搜索結果
    entity_insights: List[Dict[str, Any]] = field(default_factory=list)  # 實體洞察
    relationship_chains: List[str] = field(default_factory=list)  # 關係鏈
    
    # 統計信息
    total_facts: int = 0
    total_entities: int = 0
    total_relationships: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "simulation_requirement": self.simulation_requirement,
            "sub_queries": self.sub_queries,
            "semantic_facts": self.semantic_facts,
            "entity_insights": self.entity_insights,
            "relationship_chains": self.relationship_chains,
            "total_facts": self.total_facts,
            "total_entities": self.total_entities,
            "total_relationships": self.total_relationships
        }
    
    def to_text(self) -> str:
        """轉換為詳細的文本格式，供LLM理解"""
        text_parts = [
            f"## 未來預測深度分析",
            f"分析問題: {self.query}",
            f"預測場景: {self.simulation_requirement}",
            f"\n### 預測數據統計",
            f"- 相關預測事實: {self.total_facts}條",
            f"- 涉及實體: {self.total_entities}個",
            f"- 關係鏈: {self.total_relationships}條"
        ]
        
        # 子問題
        if self.sub_queries:
            text_parts.append(f"\n### 分析的子問題")
            for i, sq in enumerate(self.sub_queries, 1):
                text_parts.append(f"{i}. {sq}")
        
        # 語義搜索結果
        if self.semantic_facts:
            text_parts.append(f"\n### 【關鍵事實】(請在報告中引用這些原文)")
            for i, fact in enumerate(self.semantic_facts, 1):
                text_parts.append(f"{i}. \"{fact}\"")
        
        # 實體洞察
        if self.entity_insights:
            text_parts.append(f"\n### 【核心實體】")
            for entity in self.entity_insights:
                text_parts.append(f"- **{entity.get('name', '未知')}** ({entity.get('type', '實體')})")
                if entity.get('summary'):
                    text_parts.append(f"  摘要: \"{entity.get('summary')}\"")
                if entity.get('related_facts'):
                    text_parts.append(f"  相關事實: {len(entity.get('related_facts', []))}條")
        
        # 關係鏈
        if self.relationship_chains:
            text_parts.append(f"\n### 【關係鏈】")
            for chain in self.relationship_chains:
                text_parts.append(f"- {chain}")
        
        return "\n".join(text_parts)


@dataclass
class PanoramaResult:
    """
    廣度搜索結果 (Panorama)
    包含所有相關信息，包括過期內容
    """
    query: str
    
    # 全部節點
    all_nodes: List[NodeInfo] = field(default_factory=list)
    # 全部邊（包括過期的）
    all_edges: List[EdgeInfo] = field(default_factory=list)
    # 當前有效的事實
    active_facts: List[str] = field(default_factory=list)
    # 已過期/失效的事實（歷史記錄）
    historical_facts: List[str] = field(default_factory=list)
    
    # 統計
    total_nodes: int = 0
    total_edges: int = 0
    active_count: int = 0
    historical_count: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "all_nodes": [n.to_dict() for n in self.all_nodes],
            "all_edges": [e.to_dict() for e in self.all_edges],
            "active_facts": self.active_facts,
            "historical_facts": self.historical_facts,
            "total_nodes": self.total_nodes,
            "total_edges": self.total_edges,
            "active_count": self.active_count,
            "historical_count": self.historical_count
        }
    
    def to_text(self) -> str:
        """轉換為文本格式（完整版本，不截斷）"""
        text_parts = [
            f"## 廣度搜索結果（未來全景視圖）",
            f"查詢: {self.query}",
            f"\n### 統計信息",
            f"- 總節點數: {self.total_nodes}",
            f"- 總邊數: {self.total_edges}",
            f"- 當前有效事實: {self.active_count}條",
            f"- 歷史/過期事實: {self.historical_count}條"
        ]
        
        # 當前有效的事實（完整輸出，不截斷）
        if self.active_facts:
            text_parts.append(f"\n### 【當前有效事實】(模擬結果原文)")
            for i, fact in enumerate(self.active_facts, 1):
                text_parts.append(f"{i}. \"{fact}\"")
        
        # 歷史/過期事實（完整輸出，不截斷）
        if self.historical_facts:
            text_parts.append(f"\n### 【歷史/過期事實】(演變過程記錄)")
            for i, fact in enumerate(self.historical_facts, 1):
                text_parts.append(f"{i}. \"{fact}\"")
        
        # 關鍵實體（完整輸出，不截斷）
        if self.all_nodes:
            text_parts.append(f"\n### 【涉及實體】")
            for node in self.all_nodes:
                entity_type = next((l for l in node.labels if l not in ["Entity", "Node"]), "實體")
                text_parts.append(f"- **{node.name}** ({entity_type})")
        
        return "\n".join(text_parts)


@dataclass
class AgentInterview:
    """單個Agent的採訪結果"""
    agent_name: str
    agent_role: str  # 角色類型（如：學生、教師、媒體等）
    agent_bio: str  # 簡介
    question: str  # 採訪問題
    response: str  # 採訪回答
    key_quotes: List[str] = field(default_factory=list)  # 關鍵引言
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "agent_role": self.agent_role,
            "agent_bio": self.agent_bio,
            "question": self.question,
            "response": self.response,
            "key_quotes": self.key_quotes
        }
    
    def to_text(self) -> str:
        text = f"**{self.agent_name}** ({self.agent_role})\n"
        # 顯示完整的agent_bio，不截斷
        text += f"_簡介: {self.agent_bio}_\n\n"
        text += f"**Q:** {self.question}\n\n"
        text += f"**A:** {self.response}\n"
        if self.key_quotes:
            text += "\n**關鍵引言:**\n"
            for quote in self.key_quotes:
                # 清理各種引號
                clean_quote = quote.replace('\u201c', '').replace('\u201d', '').replace('"', '')
                clean_quote = clean_quote.replace('\u300c', '').replace('\u300d', '')
                clean_quote = clean_quote.strip()
                # 去掉開頭的標點
                while clean_quote and clean_quote[0] in '，,；;：:、。！？\n\r\t ':
                    clean_quote = clean_quote[1:]
                # 過濾包含問題編號的垃圾內容（問題1-9）
                skip = False
                for d in '123456789':
                    if f'\u95ee\u9898{d}' in clean_quote:
                        skip = True
                        break
                if skip:
                    continue
                # 截斷過長內容（按句號截斷，而非硬截斷）
                if len(clean_quote) > 150:
                    dot_pos = clean_quote.find('\u3002', 80)
                    if dot_pos > 0:
                        clean_quote = clean_quote[:dot_pos + 1]
                    else:
                        clean_quote = clean_quote[:147] + "..."
                if clean_quote and len(clean_quote) >= 10:
                    text += f'> "{clean_quote}"\n'
        return text


@dataclass
class InterviewResult:
    """
    採訪結果 (Interview)
    包含多個模擬Agent的採訪回答
    """
    interview_topic: str  # 採訪主題
    interview_questions: List[str]  # 採訪問題列表
    
    # 採訪選擇的Agent
    selected_agents: List[Dict[str, Any]] = field(default_factory=list)
    # 各Agent的採訪回答
    interviews: List[AgentInterview] = field(default_factory=list)
    
    # 選擇Agent的理由
    selection_reasoning: str = ""
    # 整合後的採訪摘要
    summary: str = ""
    
    # 統計
    total_agents: int = 0
    interviewed_count: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "interview_topic": self.interview_topic,
            "interview_questions": self.interview_questions,
            "selected_agents": self.selected_agents,
            "interviews": [i.to_dict() for i in self.interviews],
            "selection_reasoning": self.selection_reasoning,
            "summary": self.summary,
            "total_agents": self.total_agents,
            "interviewed_count": self.interviewed_count
        }
    
    def to_text(self) -> str:
        """轉換為詳細的文本格式，供LLM理解和報告引用"""
        text_parts = [
            "## 深度採訪報告",
            f"**採訪主題:** {self.interview_topic}",
            f"**採訪人數:** {self.interviewed_count} / {self.total_agents} 位模擬Agent",
            "\n### 採訪對象選擇理由",
            self.selection_reasoning or "（自動選擇）",
            "\n---",
            "\n### 採訪實錄",
        ]

        if self.interviews:
            for i, interview in enumerate(self.interviews, 1):
                text_parts.append(f"\n#### 採訪 #{i}: {interview.agent_name}")
                text_parts.append(interview.to_text())
                text_parts.append("\n---")
        else:
            text_parts.append("（無採訪記錄）\n\n---")

        text_parts.append("\n### 採訪摘要與核心觀點")
        text_parts.append(self.summary or "（無摘要）")

        return "\n".join(text_parts)


class ZepToolsService:
    """
    Zep檢索工具服務
    
    【核心檢索工具 - 優化後】
    1. insight_forge - 深度洞察檢索（最強大，自動生成子問題，多維度檢索）
    2. panorama_search - 廣度搜索（獲取全貌，包括過期內容）
    3. quick_search - 簡單搜索（快速檢索）
    4. interview_agents - 深度採訪（採訪模擬Agent，獲取多視角觀點）
    
    【基礎工具】
    - search_graph - 圖譜語義搜索
    - get_all_nodes - 獲取圖譜所有節點
    - get_all_edges - 獲取圖譜所有邊（含時間信息）
    - get_node_detail - 獲取節點詳細信息
    - get_node_edges - 獲取節點相關的邊
    - get_entities_by_type - 按類型獲取實體
    - get_entity_summary - 獲取實體的關係摘要
    """
    
    # 重試配置
    MAX_RETRIES = 3
    RETRY_DELAY = 2.0
    
    def __init__(self, api_key: Optional[str] = None, llm_client: Optional[LLMClient] = None):
        self.use_local = Config.MEMORY_BACKEND != "zep"
        self.local_store = LocalGraphStore() if self.use_local else None
        self.api_key = api_key or Config.ZEP_API_KEY
        if self.use_local:
            self.client = None
            self._llm_client = llm_client
            logger.info("Local graph tools initialized")
            return
        if not self.api_key:
            raise ValueError("ZEP_API_KEY 未配置")
        if Zep is None:
            raise ValueError("zep-cloud dependency is not installed")
        
        self.client = Zep(api_key=self.api_key)
        # LLM客戶端用於InsightForge生成子問題
        self._llm_client = llm_client
        logger.info(t("console.zepToolsInitialized"))
    
    @property
    def llm(self) -> LLMClient:
        """延遲初始化LLM客戶端"""
        if self._llm_client is None:
            self._llm_client = LLMClient()
        return self._llm_client
    
    def _call_with_retry(self, func, operation_name: str, max_retries: int = None):
        """帶重試機制的API調用"""
        max_retries = max_retries or self.MAX_RETRIES
        last_exception = None
        delay = self.RETRY_DELAY
        
        for attempt in range(max_retries):
            try:
                return func()
            except Exception as e:
                last_exception = e
                if attempt < max_retries - 1:
                    logger.warning(
                        t("console.zepRetryAttempt", operation=operation_name, attempt=attempt + 1, error=str(e)[:100], delay=f"{delay:.1f}")
                    )
                    time.sleep(delay)
                    delay *= 2
                else:
                    logger.error(t("console.zepAllRetriesFailed", operation=operation_name, retries=max_retries, error=str(e)))
        
        raise last_exception
    
    def search_graph(
        self, 
        graph_id: str, 
        query: str, 
        limit: int = 10,
        scope: str = "edges"
    ) -> SearchResult:
        """
        圖譜語義搜索
        
        使用混合搜索（語義+BM25）在圖譜中搜索相關信息。
        如果Zep Cloud的search API不可用，則降級為本地關鍵詞匹配。
        
        Args:
            graph_id: 圖譜ID (Standalone Graph)
            query: 搜索查詢
            limit: 返回結果數量
            scope: 搜索範圍，"edges" 或 "nodes"
            
        Returns:
            SearchResult: 搜索結果
        """
        logger.info(t("console.graphSearch", graphId=graph_id, query=query[:50]))

        if self.use_local:
            return self._local_search(graph_id, query, limit, "both" if scope == "both" else scope)
        
        # 嘗試使用Zep Cloud Search API
        try:
            search_results = self._call_with_retry(
                func=lambda: self.client.graph.search(
                    graph_id=graph_id,
                    query=query,
                    limit=limit,
                    scope=scope,
                    reranker="cross_encoder"
                ),
                operation_name=t("console.graphSearchOp", graphId=graph_id)
            )
            
            facts = []
            edges = []
            nodes = []
            
            # 解析邊搜索結果
            if hasattr(search_results, 'edges') and search_results.edges:
                for edge in search_results.edges:
                    if hasattr(edge, 'fact') and edge.fact:
                        facts.append(edge.fact)
                    edges.append({
                        "uuid": getattr(edge, 'uuid_', None) or getattr(edge, 'uuid', ''),
                        "name": getattr(edge, 'name', ''),
                        "fact": getattr(edge, 'fact', ''),
                        "source_node_uuid": getattr(edge, 'source_node_uuid', ''),
                        "target_node_uuid": getattr(edge, 'target_node_uuid', ''),
                    })
            
            # 解析節點搜索結果
            if hasattr(search_results, 'nodes') and search_results.nodes:
                for node in search_results.nodes:
                    nodes.append({
                        "uuid": getattr(node, 'uuid_', None) or getattr(node, 'uuid', ''),
                        "name": getattr(node, 'name', ''),
                        "labels": getattr(node, 'labels', []),
                        "summary": getattr(node, 'summary', ''),
                    })
                    # 節點摘要也算作事實
                    if hasattr(node, 'summary') and node.summary:
                        facts.append(f"[{node.name}]: {node.summary}")
            
            logger.info(t("console.searchComplete", count=len(facts)))
            
            return SearchResult(
                facts=facts,
                edges=edges,
                nodes=nodes,
                query=query,
                total_count=len(facts)
            )
            
        except Exception as e:
            logger.warning(t("console.zepSearchApiFallback", error=str(e)))
            # 降級：使用本地關鍵詞匹配搜索
            return self._local_search(graph_id, query, limit, scope)
    
    def _local_search(
        self, 
        graph_id: str, 
        query: str, 
        limit: int = 10,
        scope: str = "edges"
    ) -> SearchResult:
        """
        本地關鍵詞匹配搜索（作為Zep Search API的降級方案）
        
        獲取所有邊/節點，然後在本地進行關鍵詞匹配
        
        Args:
            graph_id: 圖譜ID
            query: 搜索查詢
            limit: 返回結果數量
            scope: 搜索範圍
            
        Returns:
            SearchResult: 搜索結果
        """
        logger.info(t("console.usingLocalSearch", query=query[:30]))
        
        facts = []
        edges_result = []
        nodes_result = []
        
        # 提取查詢關鍵詞（簡單分詞）
        query_lower = query.lower()
        keywords = [w.strip() for w in query_lower.replace(',', ' ').replace('，', ' ').split() if len(w.strip()) > 1]
        
        def match_score(text: str) -> int:
            """計算文本與查詢的匹配分數"""
            if not text:
                return 0
            text_lower = text.lower()
            # 完全匹配查詢
            if query_lower in text_lower:
                return 100
            # 關鍵詞匹配
            score = 0
            for keyword in keywords:
                if keyword in text_lower:
                    score += 10
            return score
        
        try:
            if self.use_local and hasattr(self.local_store, "search_context"):
                context = self.local_store.search_context(graph_id, query, limit)
                for edge in context.get("edges", [])[:limit]:
                    evidence = edge.get("evidence", []) or []
                    refs = [
                        f"{ev.get('source_file') or ev.get('chunk_id')}: {ev.get('quote_or_span', '')}"
                        for ev in evidence[:2]
                        if ev.get("quote_or_span")
                    ]
                    fact = edge.get("fact", "")
                    if refs:
                        fact = f"{fact}\nEvidence: " + " | ".join(refs)
                    if fact:
                        facts.append(fact)
                    edges_result.append({
                        "uuid": edge.get("uuid", ""),
                        "name": edge.get("name", ""),
                        "fact": edge.get("fact", ""),
                        "source_node_uuid": edge.get("source_node_uuid", ""),
                        "target_node_uuid": edge.get("target_node_uuid", ""),
                        "evidence": evidence,
                    })

                for node in context.get("nodes", [])[:limit]:
                    evidence = node.get("evidence", []) or []
                    nodes_result.append({
                        "uuid": node.get("uuid", ""),
                        "name": node.get("name", ""),
                        "labels": node.get("labels", []),
                        "summary": node.get("summary", ""),
                        "evidence": evidence,
                    })
                    if node.get("summary"):
                        facts.append(f"[{node.get('name', '')}]: {node.get('summary', '')}")

                for chunk in context.get("chunks", [])[: max(0, limit - len(facts))]:
                    snippet = " ".join(chunk.get("data", "").split())[:500]
                    if snippet:
                        facts.append(f"[{chunk.get('source_file') or chunk.get('uuid')}]: {snippet}")

                return SearchResult(
                    facts=facts[:limit],
                    edges=edges_result[:limit],
                    nodes=nodes_result[:limit],
                    query=query,
                    total_count=len(facts)
                )

            if scope in ["edges", "both"]:
                # 獲取所有邊並匹配
                all_edges = self.get_all_edges(graph_id)
                scored_edges = []
                for edge in all_edges:
                    score = match_score(edge.fact) + match_score(edge.name)
                    if score > 0:
                        scored_edges.append((score, edge))
                
                # 按分數排序
                scored_edges.sort(key=lambda x: x[0], reverse=True)
                
                for score, edge in scored_edges[:limit]:
                    if edge.fact:
                        facts.append(edge.fact)
                    edges_result.append({
                        "uuid": edge.uuid,
                        "name": edge.name,
                        "fact": edge.fact,
                        "source_node_uuid": edge.source_node_uuid,
                        "target_node_uuid": edge.target_node_uuid,
                    })
            
            if scope in ["nodes", "both"]:
                # 獲取所有節點並匹配
                all_nodes = self.get_all_nodes(graph_id)
                scored_nodes = []
                for node in all_nodes:
                    score = match_score(node.name) + match_score(node.summary)
                    if score > 0:
                        scored_nodes.append((score, node))
                
                scored_nodes.sort(key=lambda x: x[0], reverse=True)
                
                for score, node in scored_nodes[:limit]:
                    nodes_result.append({
                        "uuid": node.uuid,
                        "name": node.name,
                        "labels": node.labels,
                        "summary": node.summary,
                    })
                    if node.summary:
                        facts.append(f"[{node.name}]: {node.summary}")
            
            logger.info(t("console.localSearchComplete", count=len(facts)))
            
        except Exception as e:
            logger.error(t("console.localSearchFailed", error=str(e)))
        
        return SearchResult(
            facts=facts,
            edges=edges_result,
            nodes=nodes_result,
            query=query,
            total_count=len(facts)
        )
    
    def get_all_nodes(self, graph_id: str) -> List[NodeInfo]:
        """
        獲取圖譜的所有節點（分頁獲取）

        Args:
            graph_id: 圖譜ID

        Returns:
            節點列表
        """
        logger.info(t("console.fetchingAllNodes", graphId=graph_id))

        if self.use_local:
            result = []
            for node in self.local_store.graph_data(graph_id)["nodes"]:
                result.append(NodeInfo(
                    uuid=str(node.get("uuid", "")),
                    name=node.get("name", ""),
                    labels=node.get("labels", []),
                    summary=node.get("summary", ""),
                    attributes=node.get("attributes", {}),
                ))
            logger.info(t("console.fetchedNodes", count=len(result)))
            return result

        nodes = fetch_all_nodes(self.client, graph_id)

        result = []
        for node in nodes:
            node_uuid = getattr(node, 'uuid_', None) or getattr(node, 'uuid', None) or ""
            result.append(NodeInfo(
                uuid=str(node_uuid) if node_uuid else "",
                name=node.name or "",
                labels=node.labels or [],
                summary=node.summary or "",
                attributes=node.attributes or {}
            ))

        logger.info(t("console.fetchedNodes", count=len(result)))
        return result

    def get_all_edges(self, graph_id: str, include_temporal: bool = True) -> List[EdgeInfo]:
        """
        獲取圖譜的所有邊（分頁獲取，包含時間信息）

        Args:
            graph_id: 圖譜ID
            include_temporal: 是否包含時間信息（默認True）

        Returns:
            邊列表（包含created_at, valid_at, invalid_at, expired_at）
        """
        logger.info(t("console.fetchingAllEdges", graphId=graph_id))

        if self.use_local:
            result = []
            for edge in self.local_store.graph_data(graph_id)["edges"]:
                edge_info = EdgeInfo(
                    uuid=str(edge.get("uuid", "")),
                    name=edge.get("name", ""),
                    fact=edge.get("fact", ""),
                    source_node_uuid=edge.get("source_node_uuid", ""),
                    target_node_uuid=edge.get("target_node_uuid", ""),
                    source_node_name=edge.get("source_node_name"),
                    target_node_name=edge.get("target_node_name"),
                )
                if include_temporal:
                    edge_info.created_at = edge.get("created_at")
                    edge_info.valid_at = edge.get("valid_at")
                    edge_info.invalid_at = edge.get("invalid_at")
                    edge_info.expired_at = edge.get("expired_at")
                result.append(edge_info)
            logger.info(t("console.fetchedEdges", count=len(result)))
            return result

        edges = fetch_all_edges(self.client, graph_id)

        result = []
        for edge in edges:
            edge_uuid = getattr(edge, 'uuid_', None) or getattr(edge, 'uuid', None) or ""
            edge_info = EdgeInfo(
                uuid=str(edge_uuid) if edge_uuid else "",
                name=edge.name or "",
                fact=edge.fact or "",
                source_node_uuid=edge.source_node_uuid or "",
                target_node_uuid=edge.target_node_uuid or ""
            )

            # 添加時間信息
            if include_temporal:
                edge_info.created_at = getattr(edge, 'created_at', None)
                edge_info.valid_at = getattr(edge, 'valid_at', None)
                edge_info.invalid_at = getattr(edge, 'invalid_at', None)
                edge_info.expired_at = getattr(edge, 'expired_at', None)

            result.append(edge_info)

        logger.info(t("console.fetchedEdges", count=len(result)))
        return result
    
    def get_node_detail(self, node_uuid: str) -> Optional[NodeInfo]:
        """
        獲取單個節點的詳細信息
        
        Args:
            node_uuid: 節點UUID
            
        Returns:
            節點信息或None
        """
        logger.info(t("console.fetchingNodeDetail", uuid=node_uuid[:8]))
        
        try:
            if self.use_local:
                graph_ids = [
                    filename[:-5]
                    for filename in os.listdir(self.local_store.base_dir)
                    if filename.endswith(".json")
                ]
                for graph_id in graph_ids:
                    for node in self.local_store.graph_data(graph_id)["nodes"]:
                        if node.get("uuid") == node_uuid:
                            return NodeInfo(
                                uuid=node.get("uuid", ""),
                                name=node.get("name", ""),
                                labels=node.get("labels", []),
                                summary=node.get("summary", ""),
                                attributes=node.get("attributes", {}),
                            )
                return None

            node = self._call_with_retry(
                func=lambda: self.client.graph.node.get(uuid_=node_uuid),
                operation_name=t("console.fetchNodeDetailOp", uuid=node_uuid[:8])
            )
            
            if not node:
                return None
            
            return NodeInfo(
                uuid=getattr(node, 'uuid_', None) or getattr(node, 'uuid', ''),
                name=node.name or "",
                labels=node.labels or [],
                summary=node.summary or "",
                attributes=node.attributes or {}
            )
        except Exception as e:
            logger.error(t("console.fetchNodeDetailFailed", error=str(e)))
            return None
    
    def get_node_edges(self, graph_id: str, node_uuid: str) -> List[EdgeInfo]:
        """
        獲取節點相關的所有邊
        
        通過獲取圖譜所有邊，然後過濾出與指定節點相關的邊
        
        Args:
            graph_id: 圖譜ID
            node_uuid: 節點UUID
            
        Returns:
            邊列表
        """
        logger.info(t("console.fetchingNodeEdges", uuid=node_uuid[:8]))
        
        try:
            # 獲取圖譜所有邊，然後過濾
            all_edges = self.get_all_edges(graph_id)
            
            result = []
            for edge in all_edges:
                # 檢查邊是否與指定節點相關（作為源或目標）
                if edge.source_node_uuid == node_uuid or edge.target_node_uuid == node_uuid:
                    result.append(edge)
            
            logger.info(t("console.foundNodeEdges", count=len(result)))
            return result
            
        except Exception as e:
            logger.warning(t("console.fetchNodeEdgesFailed", error=str(e)))
            return []
    
    def get_entities_by_type(
        self, 
        graph_id: str, 
        entity_type: str
    ) -> List[NodeInfo]:
        """
        按類型獲取實體
        
        Args:
            graph_id: 圖譜ID
            entity_type: 實體類型（如 Student, PublicFigure 等）
            
        Returns:
            符合類型的實體列表
        """
        logger.info(t("console.fetchingEntitiesByType", type=entity_type))
        
        all_nodes = self.get_all_nodes(graph_id)
        
        filtered = []
        for node in all_nodes:
            # 檢查labels是否包含指定類型
            if entity_type in node.labels:
                filtered.append(node)
        
        logger.info(t("console.foundEntitiesByType", count=len(filtered), type=entity_type))
        return filtered
    
    def get_entity_summary(
        self, 
        graph_id: str, 
        entity_name: str
    ) -> Dict[str, Any]:
        """
        獲取指定實體的關係摘要
        
        搜索與該實體相關的所有信息，並生成摘要
        
        Args:
            graph_id: 圖譜ID
            entity_name: 實體名稱
            
        Returns:
            實體摘要信息
        """
        logger.info(t("console.fetchingEntitySummary", name=entity_name))
        
        # 先搜索該實體相關的信息
        search_result = self.search_graph(
            graph_id=graph_id,
            query=entity_name,
            limit=20
        )
        
        # 嘗試在所有節點中找到該實體
        all_nodes = self.get_all_nodes(graph_id)
        entity_node = None
        for node in all_nodes:
            if node.name.lower() == entity_name.lower():
                entity_node = node
                break
        
        related_edges = []
        if entity_node:
            # 傳入graph_id參數
            related_edges = self.get_node_edges(graph_id, entity_node.uuid)
        
        return {
            "entity_name": entity_name,
            "entity_info": entity_node.to_dict() if entity_node else None,
            "related_facts": search_result.facts,
            "related_edges": [e.to_dict() for e in related_edges],
            "total_relations": len(related_edges)
        }
    
    def get_graph_statistics(self, graph_id: str) -> Dict[str, Any]:
        """
        獲取圖譜的統計信息
        
        Args:
            graph_id: 圖譜ID
            
        Returns:
            統計信息
        """
        logger.info(t("console.fetchingGraphStats", graphId=graph_id))
        
        nodes = self.get_all_nodes(graph_id)
        edges = self.get_all_edges(graph_id)
        
        # 統計實體類型分佈
        entity_types = {}
        for node in nodes:
            for label in node.labels:
                if label not in ["Entity", "Node"]:
                    entity_types[label] = entity_types.get(label, 0) + 1
        
        # 統計關係類型分佈
        relation_types = {}
        for edge in edges:
            relation_types[edge.name] = relation_types.get(edge.name, 0) + 1
        
        return {
            "graph_id": graph_id,
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "entity_types": entity_types,
            "relation_types": relation_types
        }
    
    def get_simulation_context(
        self, 
        graph_id: str,
        simulation_requirement: str,
        limit: int = 30
    ) -> Dict[str, Any]:
        """
        獲取模擬相關的上下文信息
        
        綜合搜索與模擬需求相關的所有信息
        
        Args:
            graph_id: 圖譜ID
            simulation_requirement: 模擬需求描述
            limit: 每類信息的數量限制
            
        Returns:
            模擬上下文信息
        """
        logger.info(t("console.fetchingSimContext", requirement=simulation_requirement[:50]))
        
        # 搜索與模擬需求相關的信息
        search_result = self.search_graph(
            graph_id=graph_id,
            query=simulation_requirement,
            limit=limit
        )
        
        # 獲取圖譜統計
        stats = self.get_graph_statistics(graph_id)
        
        # 獲取所有實體節點
        all_nodes = self.get_all_nodes(graph_id)
        
        # 篩選有實際類型的實體（非純Entity節點）
        entities = []
        for node in all_nodes:
            custom_labels = [l for l in node.labels if l not in ["Entity", "Node"]]
            if custom_labels:
                entities.append({
                    "name": node.name,
                    "type": custom_labels[0],
                    "summary": node.summary
                })
        
        return {
            "simulation_requirement": simulation_requirement,
            "related_facts": search_result.facts,
            "graph_statistics": stats,
            "entities": entities[:limit],  # 限制數量
            "total_entities": len(entities)
        }
    
    # ========== 核心檢索工具（優化後） ==========
    
    def insight_forge(
        self,
        graph_id: str,
        query: str,
        simulation_requirement: str,
        report_context: str = "",
        max_sub_queries: int = 5
    ) -> InsightForgeResult:
        """
        【InsightForge - 深度洞察檢索】
        
        最強大的混合檢索函數，自動分解問題並多維度檢索：
        1. 使用LLM將問題分解為多個子問題
        2. 對每個子問題進行語義搜索
        3. 提取相關實體並獲取其詳細信息
        4. 追蹤關係鏈
        5. 整合所有結果，生成深度洞察
        
        Args:
            graph_id: 圖譜ID
            query: 用戶問題
            simulation_requirement: 模擬需求描述
            report_context: 報告上下文（可選，用於更精準的子問題生成）
            max_sub_queries: 最大子問題數量
            
        Returns:
            InsightForgeResult: 深度洞察檢索結果
        """
        logger.info(t("console.insightForgeStart", query=query[:50]))
        
        result = InsightForgeResult(
            query=query,
            simulation_requirement=simulation_requirement,
            sub_queries=[]
        )
        
        # Step 1: 使用LLM生成子問題
        sub_queries = self._generate_sub_queries(
            query=query,
            simulation_requirement=simulation_requirement,
            report_context=report_context,
            max_queries=max_sub_queries
        )
        result.sub_queries = sub_queries
        logger.info(t("console.generatedSubQueries", count=len(sub_queries)))
        
        # Step 2: 對每個子問題進行語義搜索
        all_facts = []
        all_edges = []
        seen_facts = set()
        
        for sub_query in sub_queries:
            search_result = self.search_graph(
                graph_id=graph_id,
                query=sub_query,
                limit=15,
                scope="edges"
            )
            
            for fact in search_result.facts:
                if fact not in seen_facts:
                    all_facts.append(fact)
                    seen_facts.add(fact)
            
            all_edges.extend(search_result.edges)
        
        # 對原始問題也進行搜索
        main_search = self.search_graph(
            graph_id=graph_id,
            query=query,
            limit=20,
            scope="edges"
        )
        for fact in main_search.facts:
            if fact not in seen_facts:
                all_facts.append(fact)
                seen_facts.add(fact)
        
        result.semantic_facts = all_facts
        result.total_facts = len(all_facts)
        
        # Step 3: 從邊中提取相關實體UUID，只獲取這些實體的信息（不獲取全部節點）
        entity_uuids = set()
        for edge_data in all_edges:
            if isinstance(edge_data, dict):
                source_uuid = edge_data.get('source_node_uuid', '')
                target_uuid = edge_data.get('target_node_uuid', '')
                if source_uuid:
                    entity_uuids.add(source_uuid)
                if target_uuid:
                    entity_uuids.add(target_uuid)
        
        # 獲取所有相關實體的詳情（不限制數量，完整輸出）
        entity_insights = []
        node_map = {}  # 用於後續關係鏈構建
        
        for uuid in list(entity_uuids):  # 處理所有實體，不截斷
            if not uuid:
                continue
            try:
                # 單獨獲取每個相關節點的信息
                node = self.get_node_detail(uuid)
                if node:
                    node_map[uuid] = node
                    entity_type = next((l for l in node.labels if l not in ["Entity", "Node"]), "實體")
                    
                    # 獲取該實體相關的所有事實（不截斷）
                    related_facts = [
                        f for f in all_facts 
                        if node.name.lower() in f.lower()
                    ]
                    
                    entity_insights.append({
                        "uuid": node.uuid,
                        "name": node.name,
                        "type": entity_type,
                        "summary": node.summary,
                        "related_facts": related_facts  # 完整輸出，不截斷
                    })
            except Exception as e:
                logger.debug(f"獲取節點 {uuid} 失敗: {e}")
                continue
        
        result.entity_insights = entity_insights
        result.total_entities = len(entity_insights)
        
        # Step 4: 構建所有關係鏈（不限制數量）
        relationship_chains = []
        for edge_data in all_edges:  # 處理所有邊，不截斷
            if isinstance(edge_data, dict):
                source_uuid = edge_data.get('source_node_uuid', '')
                target_uuid = edge_data.get('target_node_uuid', '')
                relation_name = edge_data.get('name', '')
                
                source_name = node_map.get(source_uuid, NodeInfo('', '', [], '', {})).name or source_uuid[:8]
                target_name = node_map.get(target_uuid, NodeInfo('', '', [], '', {})).name or target_uuid[:8]
                
                chain = f"{source_name} --[{relation_name}]--> {target_name}"
                if chain not in relationship_chains:
                    relationship_chains.append(chain)
        
        result.relationship_chains = relationship_chains
        result.total_relationships = len(relationship_chains)
        
        logger.info(t("console.insightForgeComplete", facts=result.total_facts, entities=result.total_entities, relationships=result.total_relationships))
        return result
    
    def _generate_sub_queries(
        self,
        query: str,
        simulation_requirement: str,
        report_context: str = "",
        max_queries: int = 5
    ) -> List[str]:
        """
        使用LLM生成子問題
        
        將複雜問題分解為多個可以獨立檢索的子問題
        """
        system_prompt = """你是一個專業的問題分析專家。你的任務是將一個複雜問題分解為多個可以在模擬世界中獨立觀察的子問題。

要求：
1. 每個子問題應該足夠具體，可以在模擬世界中找到相關的Agent行為或事件
2. 子問題應該覆蓋原問題的不同維度（如：誰、什麼、為什麼、怎麼樣、何時、何地）
3. 子問題應該與模擬場景相關
4. 返回JSON格式：{"sub_queries": ["子問題1", "子問題2", ...]}"""

        user_prompt = f"""模擬需求背景：
{simulation_requirement}

{f"報告上下文：{report_context[:500]}" if report_context else ""}

請將以下問題分解為{max_queries}個子問題：
{query}

返回JSON格式的子問題列表。"""

        try:
            response = self.llm.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3
            )
            
            sub_queries = response.get("sub_queries", [])
            # 確保是字符串列表
            return [str(sq) for sq in sub_queries[:max_queries]]
            
        except Exception as e:
            logger.warning(t("console.generateSubQueriesFailed", error=str(e)))
            # 降級：返回基於原問題的變體
            return [
                query,
                f"{query} 的主要參與者",
                f"{query} 的原因和影響",
                f"{query} 的發展過程"
            ][:max_queries]
    
    def panorama_search(
        self,
        graph_id: str,
        query: str,
        include_expired: bool = True,
        limit: int = 50
    ) -> PanoramaResult:
        """
        【PanoramaSearch - 廣度搜索】
        
        獲取全貌視圖，包括所有相關內容和歷史/過期信息：
        1. 獲取所有相關節點
        2. 獲取所有邊（包括已過期/失效的）
        3. 分類整理當前有效和歷史信息
        
        這個工具適用於需要了解事件全貌、追蹤演變過程的場景。
        
        Args:
            graph_id: 圖譜ID
            query: 搜索查詢（用於相關性排序）
            include_expired: 是否包含過期內容（默認True）
            limit: 返回結果數量限制
            
        Returns:
            PanoramaResult: 廣度搜索結果
        """
        logger.info(t("console.panoramaSearchStart", query=query[:50]))
        
        result = PanoramaResult(query=query)
        
        # 獲取所有節點
        all_nodes = self.get_all_nodes(graph_id)
        node_map = {n.uuid: n for n in all_nodes}
        result.all_nodes = all_nodes
        result.total_nodes = len(all_nodes)
        
        # 獲取所有邊（包含時間信息）
        all_edges = self.get_all_edges(graph_id, include_temporal=True)
        result.all_edges = all_edges
        result.total_edges = len(all_edges)
        
        # 分類事實
        active_facts = []
        historical_facts = []
        
        for edge in all_edges:
            if not edge.fact:
                continue
            
            # 為事實添加實體名稱
            source_name = node_map.get(edge.source_node_uuid, NodeInfo('', '', [], '', {})).name or edge.source_node_uuid[:8]
            target_name = node_map.get(edge.target_node_uuid, NodeInfo('', '', [], '', {})).name or edge.target_node_uuid[:8]
            
            # 判斷是否過期/失效
            is_historical = edge.is_expired or edge.is_invalid
            
            if is_historical:
                # 歷史/過期事實，添加時間標記
                valid_at = edge.valid_at or "未知"
                invalid_at = edge.invalid_at or edge.expired_at or "未知"
                fact_with_time = f"[{valid_at} - {invalid_at}] {edge.fact}"
                historical_facts.append(fact_with_time)
            else:
                # 當前有效事實
                active_facts.append(edge.fact)
        
        # 基於查詢進行相關性排序
        query_lower = query.lower()
        keywords = [w.strip() for w in query_lower.replace(',', ' ').replace('，', ' ').split() if len(w.strip()) > 1]
        
        def relevance_score(fact: str) -> int:
            fact_lower = fact.lower()
            score = 0
            if query_lower in fact_lower:
                score += 100
            for kw in keywords:
                if kw in fact_lower:
                    score += 10
            return score
        
        # 排序並限制數量
        active_facts.sort(key=relevance_score, reverse=True)
        historical_facts.sort(key=relevance_score, reverse=True)
        
        result.active_facts = active_facts[:limit]
        result.historical_facts = historical_facts[:limit] if include_expired else []
        result.active_count = len(active_facts)
        result.historical_count = len(historical_facts)
        
        logger.info(t("console.panoramaSearchComplete", active=result.active_count, historical=result.historical_count))
        return result
    
    def quick_search(
        self,
        graph_id: str,
        query: str,
        limit: int = 10
    ) -> SearchResult:
        """
        【QuickSearch - 簡單搜索】
        
        快速、輕量級的檢索工具：
        1. 直接調用Zep語義搜索
        2. 返回最相關的結果
        3. 適用於簡單、直接的檢索需求
        
        Args:
            graph_id: 圖譜ID
            query: 搜索查詢
            limit: 返回結果數量
            
        Returns:
            SearchResult: 搜索結果
        """
        logger.info(t("console.quickSearchStart", query=query[:50]))
        
        # 直接調用現有的search_graph方法
        result = self.search_graph(
            graph_id=graph_id,
            query=query,
            limit=limit,
            scope="edges"
        )
        
        logger.info(t("console.quickSearchComplete", count=result.total_count))
        return result
    
    def interview_agents(
        self,
        simulation_id: str,
        interview_requirement: str,
        simulation_requirement: str = "",
        max_agents: int = 5,
        custom_questions: List[str] = None
    ) -> InterviewResult:
        """
        【InterviewAgents - 深度採訪】
        
        調用真實的OASIS採訪API，採訪模擬中正在運行的Agent：
        1. 自動讀取人設文件，瞭解所有模擬Agent
        2. 使用LLM分析採訪需求，智能選擇最相關的Agent
        3. 使用LLM生成採訪問題
        4. 調用 /api/simulation/interview/batch 接口進行真實採訪（雙平臺同時採訪）
        5. 整合所有采訪結果，生成採訪報告
        
        【重要】此功能需要模擬環境處於運行狀態（OASIS環境未關閉）
        
        【使用場景】
        - 需要從不同角色視角瞭解事件看法
        - 需要收集多方意見和觀點
        - 需要獲取模擬Agent的真實回答（非LLM模擬）
        
        Args:
            simulation_id: 模擬ID（用於定位人設文件和調用採訪API）
            interview_requirement: 採訪需求描述（非結構化，如"瞭解學生對事件的看法"）
            simulation_requirement: 模擬需求背景（可選）
            max_agents: 最多采訪的Agent數量
            custom_questions: 自定義採訪問題（可選，若不提供則自動生成）
            
        Returns:
            InterviewResult: 採訪結果
        """
        from .simulation_runner import SimulationRunner
        
        logger.info(t("console.interviewAgentsStart", requirement=interview_requirement[:50]))
        
        result = InterviewResult(
            interview_topic=interview_requirement,
            interview_questions=custom_questions or []
        )
        
        # Step 1: 讀取人設文件
        profiles = self._load_agent_profiles(simulation_id)
        
        if not profiles:
            logger.warning(t("console.profilesNotFound", simId=simulation_id))
            result.summary = "未找到可採訪的Agent人設文件"
            return result
        
        result.total_agents = len(profiles)
        logger.info(t("console.loadedProfiles", count=len(profiles)))
        
        # Step 2: 使用LLM選擇要採訪的Agent（返回agent_id列表）
        selected_agents, selected_indices, selection_reasoning = self._select_agents_for_interview(
            profiles=profiles,
            interview_requirement=interview_requirement,
            simulation_requirement=simulation_requirement,
            max_agents=max_agents
        )
        
        result.selected_agents = selected_agents
        result.selection_reasoning = selection_reasoning
        logger.info(t("console.selectedAgentsForInterview", count=len(selected_agents), indices=selected_indices))
        
        # Step 3: 生成採訪問題（如果沒有提供）
        if not result.interview_questions:
            result.interview_questions = self._generate_interview_questions(
                interview_requirement=interview_requirement,
                simulation_requirement=simulation_requirement,
                selected_agents=selected_agents
            )
            logger.info(t("console.generatedInterviewQuestions", count=len(result.interview_questions)))
        
        # 將問題合併為一個採訪prompt
        combined_prompt = "\n".join([f"{i+1}. {q}" for i, q in enumerate(result.interview_questions)])
        
        # 添加優化前綴，約束Agent回覆格式
        INTERVIEW_PROMPT_PREFIX = (
            "你正在接受一次採訪。請結合你的人設、所有的過往記憶與行動，"
            "以純文本方式直接回答以下問題。\n"
            "回覆要求：\n"
            "1. 直接用自然語言回答，不要調用任何工具\n"
            "2. 不要返回JSON格式或工具調用格式\n"
            "3. 不要使用Markdown標題（如#、##、###）\n"
            "4. 按問題編號逐一回答，每個回答以「問題X：」開頭（X為問題編號）\n"
            "5. 每個問題的回答之間用空行分隔\n"
            "6. 回答要有實質內容，每個問題至少回答2-3句話\n\n"
        )
        optimized_prompt = f"{INTERVIEW_PROMPT_PREFIX}{combined_prompt}"
        
        # Step 4: 調用真實的採訪API（不指定platform，默認雙平臺同時採訪）
        try:
            # 構建批量採訪列表（不指定platform，雙平臺採訪）
            interviews_request = []
            for agent_idx in selected_indices:
                interviews_request.append({
                    "agent_id": agent_idx,
                    "prompt": optimized_prompt  # 使用優化後的prompt
                    # 不指定platform，API會在twitter和reddit兩個平臺都採訪
                })
            
            logger.info(t("console.callingBatchInterviewApi", count=len(interviews_request)))
            
            # 調用 SimulationRunner 的批量採訪方法（不傳platform，雙平臺採訪）
            api_result = SimulationRunner.interview_agents_batch(
                simulation_id=simulation_id,
                interviews=interviews_request,
                platform=None,  # 不指定platform，雙平臺採訪
                timeout=180.0   # 雙平臺需要更長超時
            )
            
            logger.info(t("console.interviewApiReturned", count=api_result.get('interviews_count', 0), success=api_result.get('success')))
            
            # 檢查API調用是否成功
            if not api_result.get("success", False):
                error_msg = api_result.get("error", "未知錯誤")
                logger.warning(t("console.interviewApiReturnedFailure", error=error_msg))
                result.summary = f"採訪API調用失敗：{error_msg}。請檢查OASIS模擬環境狀態。"
                return result
            
            # Step 5: 解析API返回結果，構建AgentInterview對象
            # 雙平臺模式返回格式: {"twitter_0": {...}, "reddit_0": {...}, "twitter_1": {...}, ...}
            api_data = api_result.get("result", {})
            results_dict = api_data.get("results", {}) if isinstance(api_data, dict) else {}
            
            for i, agent_idx in enumerate(selected_indices):
                agent = selected_agents[i]
                agent_name = agent.get("realname", agent.get("username", f"Agent_{agent_idx}"))
                agent_role = agent.get("profession", "未知")
                agent_bio = agent.get("bio", "")
                
                # 獲取該Agent在兩個平臺的採訪結果
                twitter_result = results_dict.get(f"twitter_{agent_idx}", {})
                reddit_result = results_dict.get(f"reddit_{agent_idx}", {})
                
                twitter_response = twitter_result.get("response", "")
                reddit_response = reddit_result.get("response", "")

                # 清理可能的工具調用 JSON 包裹
                twitter_response = self._clean_tool_call_response(twitter_response)
                reddit_response = self._clean_tool_call_response(reddit_response)

                # 始終輸出雙平臺標記
                twitter_text = twitter_response if twitter_response else "（該平臺未獲得回覆）"
                reddit_text = reddit_response if reddit_response else "（該平臺未獲得回覆）"
                response_text = f"【Twitter平臺回答】\n{twitter_text}\n\n【Reddit平臺回答】\n{reddit_text}"

                # 提取關鍵引言（從兩個平臺的回答中）
                import re
                combined_responses = f"{twitter_response} {reddit_response}"

                # 清理響應文本：去掉標記、編號、Markdown 等干擾
                clean_text = re.sub(r'#{1,6}\s+', '', combined_responses)
                clean_text = re.sub(r'\{[^}]*tool_name[^}]*\}', '', clean_text)
                clean_text = re.sub(r'[*_`|>~\-]{2,}', '', clean_text)
                clean_text = re.sub(r'問題\d+[：:]\s*', '', clean_text)
                clean_text = re.sub(r'【[^】]+】', '', clean_text)

                # 策略1（主）: 提取完整的有實質內容的句子
                sentences = re.split(r'[。！？]', clean_text)
                meaningful = [
                    s.strip() for s in sentences
                    if 20 <= len(s.strip()) <= 150
                    and not re.match(r'^[\s\W，,；;：:、]+', s.strip())
                    and not s.strip().startswith(('{', '問題'))
                ]
                meaningful.sort(key=len, reverse=True)
                key_quotes = [s + "。" for s in meaningful[:3]]

                # 策略2（補充）: 正確配對的中文引號「」內長文本
                if not key_quotes:
                    paired = re.findall(r'\u201c([^\u201c\u201d]{15,100})\u201d', clean_text)
                    paired += re.findall(r'\u300c([^\u300c\u300d]{15,100})\u300d', clean_text)
                    key_quotes = [q for q in paired if not re.match(r'^[，,；;：:、]', q)][:3]
                
                interview = AgentInterview(
                    agent_name=agent_name,
                    agent_role=agent_role,
                    agent_bio=agent_bio[:1000],  # 擴大bio長度限制
                    question=combined_prompt,
                    response=response_text,
                    key_quotes=key_quotes[:5]
                )
                result.interviews.append(interview)
            
            result.interviewed_count = len(result.interviews)
            
        except ValueError as e:
            # 模擬環境未運行
            logger.warning(t("console.interviewApiCallFailed", error=e))
            result.summary = f"採訪失敗：{str(e)}。模擬環境可能已關閉，請確保OASIS環境正在運行。"
            return result
        except Exception as e:
            logger.error(t("console.interviewApiCallException", error=e))
            import traceback
            logger.error(traceback.format_exc())
            result.summary = f"採訪過程發生錯誤：{str(e)}"
            return result
        
        # Step 6: 生成採訪摘要
        if result.interviews:
            result.summary = self._generate_interview_summary(
                interviews=result.interviews,
                interview_requirement=interview_requirement
            )
        
        logger.info(t("console.interviewAgentsComplete", count=result.interviewed_count))
        return result
    
    @staticmethod
    def _clean_tool_call_response(response: str) -> str:
        """清理 Agent 回覆中的 JSON 工具調用包裹，提取實際內容"""
        if not response or not response.strip().startswith('{'):
            return response
        text = response.strip()
        if 'tool_name' not in text[:80]:
            return response
        import re as _re
        try:
            data = json.loads(text)
            if isinstance(data, dict) and 'arguments' in data:
                for key in ('content', 'text', 'body', 'message', 'reply'):
                    if key in data['arguments']:
                        return str(data['arguments'][key])
        except (json.JSONDecodeError, KeyError, TypeError):
            match = _re.search(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
            if match:
                return match.group(1).replace('\\n', '\n').replace('\\"', '"')
        return response

    def _load_agent_profiles(self, simulation_id: str) -> List[Dict[str, Any]]:
        """加載模擬的Agent人設文件"""
        import os
        import csv
        
        # 構建人設文件路徑
        sim_dir = os.path.join(
            os.path.dirname(__file__), 
            f'../../uploads/simulations/{simulation_id}'
        )
        
        profiles = []
        
        # 優先嚐試讀取Reddit JSON格式
        reddit_profile_path = os.path.join(sim_dir, "reddit_profiles.json")
        if os.path.exists(reddit_profile_path):
            try:
                with open(reddit_profile_path, 'r', encoding='utf-8') as f:
                    profiles = json.load(f)
                logger.info(t("console.loadedRedditProfiles", count=len(profiles)))
                return profiles
            except Exception as e:
                logger.warning(t("console.readRedditProfilesFailed", error=e))
        
        # 嘗試讀取Twitter CSV格式
        twitter_profile_path = os.path.join(sim_dir, "twitter_profiles.csv")
        if os.path.exists(twitter_profile_path):
            try:
                with open(twitter_profile_path, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        # CSV格式轉換為統一格式
                        profiles.append({
                            "realname": row.get("name", ""),
                            "username": row.get("username", ""),
                            "bio": row.get("description", ""),
                            "persona": row.get("user_char", ""),
                            "profession": "未知"
                        })
                logger.info(t("console.loadedTwitterProfiles", count=len(profiles)))
                return profiles
            except Exception as e:
                logger.warning(t("console.readTwitterProfilesFailed", error=e))
        
        return profiles
    
    def _select_agents_for_interview(
        self,
        profiles: List[Dict[str, Any]],
        interview_requirement: str,
        simulation_requirement: str,
        max_agents: int
    ) -> tuple:
        """
        使用LLM選擇要採訪的Agent
        
        Returns:
            tuple: (selected_agents, selected_indices, reasoning)
                - selected_agents: 選中Agent的完整信息列表
                - selected_indices: 選中Agent的索引列表（用於API調用）
                - reasoning: 選擇理由
        """
        
        # 構建Agent摘要列表
        agent_summaries = []
        for i, profile in enumerate(profiles):
            summary = {
                "index": i,
                "name": profile.get("realname", profile.get("username", f"Agent_{i}")),
                "profession": profile.get("profession", "未知"),
                "bio": profile.get("bio", "")[:200],
                "interested_topics": profile.get("interested_topics", [])
            }
            agent_summaries.append(summary)
        
        system_prompt = """你是一個專業的採訪策劃專家。你的任務是根據採訪需求，從模擬Agent列表中選擇最適合採訪的對象。

選擇標準：
1. Agent的身份/職業與採訪主題相關
2. Agent可能持有獨特或有價值的觀點
3. 選擇多樣化的視角（如：支持方、反對方、中立方、專業人士等）
4. 優先選擇與事件直接相關的角色

返回JSON格式：
{
    "selected_indices": [選中Agent的索引列表],
    "reasoning": "選擇理由說明"
}"""

        user_prompt = f"""採訪需求：
{interview_requirement}

模擬背景：
{simulation_requirement if simulation_requirement else "未提供"}

可選擇的Agent列表（共{len(agent_summaries)}個）：
{json.dumps(agent_summaries, ensure_ascii=False, indent=2)}

請選擇最多{max_agents}個最適合採訪的Agent，並說明選擇理由。"""

        try:
            response = self.llm.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3
            )
            
            selected_indices = response.get("selected_indices", [])[:max_agents]
            reasoning = response.get("reasoning", "基於相關性自動選擇")
            
            # 獲取選中的Agent完整信息
            selected_agents = []
            valid_indices = []
            for idx in selected_indices:
                if 0 <= idx < len(profiles):
                    selected_agents.append(profiles[idx])
                    valid_indices.append(idx)
            
            return selected_agents, valid_indices, reasoning
            
        except Exception as e:
            logger.warning(t("console.llmSelectAgentFailed", error=e))
            # 降級：選擇前N個
            selected = profiles[:max_agents]
            indices = list(range(min(max_agents, len(profiles))))
            return selected, indices, "使用默認選擇策略"
    
    def _generate_interview_questions(
        self,
        interview_requirement: str,
        simulation_requirement: str,
        selected_agents: List[Dict[str, Any]]
    ) -> List[str]:
        """使用LLM生成採訪問題"""
        
        agent_roles = [a.get("profession", "未知") for a in selected_agents]
        
        system_prompt = """你是一個專業的記者/採訪者。根據採訪需求，生成3-5個深度採訪問題。

問題要求：
1. 開放性問題，鼓勵詳細回答
2. 針對不同角色可能有不同答案
3. 涵蓋事實、觀點、感受等多個維度
4. 語言自然，像真實採訪一樣
5. 每個問題控制在50字以內，簡潔明瞭
6. 直接提問，不要包含背景說明或前綴

返回JSON格式：{"questions": ["問題1", "問題2", ...]}"""

        user_prompt = f"""採訪需求：{interview_requirement}

模擬背景：{simulation_requirement if simulation_requirement else "未提供"}

採訪對象角色：{', '.join(agent_roles)}

請生成3-5個採訪問題。"""

        try:
            response = self.llm.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.5
            )
            
            return response.get("questions", [f"關於{interview_requirement}，您有什麼看法？"])
            
        except Exception as e:
            logger.warning(t("console.generateInterviewQuestionsFailed", error=e))
            return [
                f"關於{interview_requirement}，您的觀點是什麼？",
                "這件事對您或您所代表的群體有什麼影響？",
                "您認為應該如何解決或改進這個問題？"
            ]
    
    def _generate_interview_summary(
        self,
        interviews: List[AgentInterview],
        interview_requirement: str
    ) -> str:
        """生成採訪摘要"""
        
        if not interviews:
            return "未完成任何採訪"
        
        # 收集所有采訪內容
        interview_texts = []
        for interview in interviews:
            interview_texts.append(f"【{interview.agent_name}（{interview.agent_role}）】\n{interview.response[:500]}")
        
        quote_instruction = "引用受訪者原話時使用中文引號「」" if get_locale() == 'zh' else 'Use quotation marks "" when quoting interviewees'
        system_prompt = f"""你是一個專業的新聞編輯。請根據多位受訪者的回答，生成一份採訪摘要。

摘要要求：
1. 提煉各方主要觀點
2. 指出觀點的共識和分歧
3. 突出有價值的引言
4. 客觀中立，不偏袒任何一方
5. 控制在1000字內

格式約束（必須遵守）：
- 使用純文本段落，用空行分隔不同部分
- 不要使用Markdown標題（如#、##、###）
- 不要使用分割線（如---、***）
- {quote_instruction}
- 可以使用**加粗**標記關鍵詞，但不要使用其他Markdown語法"""

        user_prompt = f"""採訪主題：{interview_requirement}

採訪內容：
{"".join(interview_texts)}

請生成採訪摘要。"""

        try:
            summary = self.llm.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,
                max_tokens=800
            )
            return summary
            
        except Exception as e:
            logger.warning(t("console.generateInterviewSummaryFailed", error=e))
            # 降級：簡單拼接
            return f"共採訪了{len(interviews)}位受訪者，包括：" + "、".join([i.agent_name for i in interviews])
