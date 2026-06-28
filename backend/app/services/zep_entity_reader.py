"""
Zep實體讀取與過濾服務
從Zep圖譜中讀取節點，篩選出符合預定義實體類型的節點
"""

import os
import time
from typing import Dict, Any, List, Optional, Set, Callable, TypeVar
from dataclasses import dataclass, field

try:
    from zep_cloud.client import Zep
except ImportError:
    Zep = None

from ..config import Config
from ..utils.logger import get_logger
from ..utils.zep_paging import fetch_all_nodes, fetch_all_edges
from .local_graph_store import LocalGraphStore

logger = get_logger('mirofish.zep_entity_reader')

# 用於泛型返回類型
T = TypeVar('T')


@dataclass
class EntityNode:
    """實體節點數據結構"""
    uuid: str
    name: str
    labels: List[str]
    summary: str
    attributes: Dict[str, Any]
    # 相關的邊信息
    related_edges: List[Dict[str, Any]] = field(default_factory=list)
    # 相關的其他節點信息
    related_nodes: List[Dict[str, Any]] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "labels": self.labels,
            "summary": self.summary,
            "attributes": self.attributes,
            "related_edges": self.related_edges,
            "related_nodes": self.related_nodes,
        }
    
    def get_entity_type(self) -> Optional[str]:
        """獲取實體類型（排除默認的Entity標籤）"""
        for label in self.labels:
            if label not in ["Entity", "Node"]:
                return label
        return None


@dataclass
class FilteredEntities:
    """過濾後的實體集合"""
    entities: List[EntityNode]
    entity_types: Set[str]
    total_count: int
    filtered_count: int
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "entities": [e.to_dict() for e in self.entities],
            "entity_types": list(self.entity_types),
            "total_count": self.total_count,
            "filtered_count": self.filtered_count,
        }


class ZepEntityReader:
    """
    Zep實體讀取與過濾服務
    
    主要功能：
    1. 從Zep圖譜讀取所有節點
    2. 篩選出符合預定義實體類型的節點（Labels不只是Entity的節點）
    3. 獲取每個實體的相關邊和關聯節點信息
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.use_local = Config.MEMORY_BACKEND != "zep"
        self.local_store = LocalGraphStore() if self.use_local else None
        self.api_key = api_key or Config.ZEP_API_KEY
        if self.use_local:
            self.client = None
            return
        if not self.api_key:
            raise ValueError("ZEP_API_KEY 未配置")
        if Zep is None:
            raise ValueError("zep-cloud dependency is not installed")
        
        self.client = Zep(api_key=self.api_key)
    
    def _call_with_retry(
        self, 
        func: Callable[[], T], 
        operation_name: str,
        max_retries: int = 3,
        initial_delay: float = 2.0
    ) -> T:
        """
        帶重試機制的Zep API調用
        
        Args:
            func: 要執行的函數（無參數的lambda或callable）
            operation_name: 操作名稱，用於日誌
            max_retries: 最大重試次數（默認3次，即最多嘗試3次）
            initial_delay: 初始延遲秒數
            
        Returns:
            API調用結果
        """
        last_exception = None
        delay = initial_delay
        
        for attempt in range(max_retries):
            try:
                return func()
            except Exception as e:
                last_exception = e
                if attempt < max_retries - 1:
                    logger.warning(
                        f"Zep {operation_name} 第 {attempt + 1} 次嘗試失敗: {str(e)[:100]}, "
                        f"{delay:.1f}秒後重試..."
                    )
                    time.sleep(delay)
                    delay *= 2  # 指數退避
                else:
                    logger.error(f"Zep {operation_name} 在 {max_retries} 次嘗試後仍失敗: {str(e)}")
        
        raise last_exception
    
    def get_all_nodes(self, graph_id: str) -> List[Dict[str, Any]]:
        """
        獲取圖譜的所有節點（分頁獲取）

        Args:
            graph_id: 圖譜ID

        Returns:
            節點列表
        """
        logger.info(f"獲取圖譜 {graph_id} 的所有節點...")

        if self.use_local:
            nodes_data = self.local_store.graph_data(graph_id)["nodes"]
            logger.info(f"共獲取 {len(nodes_data)} 個節點")
            return nodes_data

        nodes = fetch_all_nodes(self.client, graph_id)

        nodes_data = []
        for node in nodes:
            nodes_data.append({
                "uuid": getattr(node, 'uuid_', None) or getattr(node, 'uuid', ''),
                "name": node.name or "",
                "labels": node.labels or [],
                "summary": node.summary or "",
                "attributes": node.attributes or {},
            })

        logger.info(f"共獲取 {len(nodes_data)} 個節點")
        return nodes_data

    def get_all_edges(self, graph_id: str) -> List[Dict[str, Any]]:
        """
        獲取圖譜的所有邊（分頁獲取）

        Args:
            graph_id: 圖譜ID

        Returns:
            邊列表
        """
        logger.info(f"獲取圖譜 {graph_id} 的所有邊...")

        if self.use_local:
            edges_data = self.local_store.graph_data(graph_id)["edges"]
            logger.info(f"共獲取 {len(edges_data)} 條邊")
            return edges_data

        edges = fetch_all_edges(self.client, graph_id)

        edges_data = []
        for edge in edges:
            edges_data.append({
                "uuid": getattr(edge, 'uuid_', None) or getattr(edge, 'uuid', ''),
                "name": edge.name or "",
                "fact": edge.fact or "",
                "source_node_uuid": edge.source_node_uuid,
                "target_node_uuid": edge.target_node_uuid,
                "attributes": edge.attributes or {},
            })

        logger.info(f"共獲取 {len(edges_data)} 條邊")
        return edges_data
    
    def get_node_edges(self, node_uuid: str) -> List[Dict[str, Any]]:
        """
        獲取指定節點的所有相關邊（帶重試機制）
        
        Args:
            node_uuid: 節點UUID
            
        Returns:
            邊列表
        """
        try:
            if self.use_local:
                edges = []
                graph_ids = [
                    filename[:-5]
                    for filename in os.listdir(self.local_store.base_dir)
                    if filename.endswith(".json")
                ]
                for graph_id in graph_ids:
                    for edge in self.local_store.graph_data(graph_id)["edges"]:
                        if edge.get("source_node_uuid") == node_uuid or edge.get("target_node_uuid") == node_uuid:
                            edges.append(edge)
                return edges

            # 使用重試機制調用Zep API
            edges = self._call_with_retry(
                func=lambda: self.client.graph.node.get_entity_edges(node_uuid=node_uuid),
                operation_name=f"獲取節點邊(node={node_uuid[:8]}...)"
            )
            
            edges_data = []
            for edge in edges:
                edges_data.append({
                    "uuid": getattr(edge, 'uuid_', None) or getattr(edge, 'uuid', ''),
                    "name": edge.name or "",
                    "fact": edge.fact or "",
                    "source_node_uuid": edge.source_node_uuid,
                    "target_node_uuid": edge.target_node_uuid,
                    "attributes": edge.attributes or {},
                })
            
            return edges_data
        except Exception as e:
            logger.warning(f"獲取節點 {node_uuid} 的邊失敗: {str(e)}")
            return []
    
    def filter_defined_entities(
        self, 
        graph_id: str,
        defined_entity_types: Optional[List[str]] = None,
        enrich_with_edges: bool = True
    ) -> FilteredEntities:
        """
        篩選出符合預定義實體類型的節點
        
        篩選邏輯：
        - 如果節點的Labels只有一個"Entity"，說明這個實體不符合我們預定義的類型，跳過
        - 如果節點的Labels包含除"Entity"和"Node"之外的標籤，說明符合預定義類型，保留
        
        Args:
            graph_id: 圖譜ID
            defined_entity_types: 預定義的實體類型列表（可選，如果提供則只保留這些類型）
            enrich_with_edges: 是否獲取每個實體的相關邊信息
            
        Returns:
            FilteredEntities: 過濾後的實體集合
        """
        logger.info(f"開始篩選圖譜 {graph_id} 的實體...")
        
        # 獲取所有節點
        all_nodes = self.get_all_nodes(graph_id)
        total_count = len(all_nodes)
        
        # 獲取所有邊（用於後續關聯查找）
        all_edges = self.get_all_edges(graph_id) if enrich_with_edges else []
        
        # 構建節點UUID到節點數據的映射
        node_map = {n["uuid"]: n for n in all_nodes}
        
        # 篩選符合條件的實體
        filtered_entities = []
        entity_types_found = set()
        
        for node in all_nodes:
            labels = node.get("labels", [])
            
            # 篩選邏輯：Labels必須包含除"Entity"和"Node"之外的標籤
            custom_labels = [l for l in labels if l not in ["Entity", "Node"]]
            
            if not custom_labels:
                # 只有默認標籤，跳過
                continue
            
            # 如果指定了預定義類型，檢查是否匹配
            if defined_entity_types:
                matching_labels = [l for l in custom_labels if l in defined_entity_types]
                if not matching_labels:
                    continue
                entity_type = matching_labels[0]
            else:
                entity_type = custom_labels[0]
            
            entity_types_found.add(entity_type)
            
            # 創建實體節點對象
            entity = EntityNode(
                uuid=node["uuid"],
                name=node["name"],
                labels=labels,
                summary=node["summary"],
                attributes=node["attributes"],
            )
            
            # 獲取相關邊和節點
            if enrich_with_edges:
                related_edges = []
                related_node_uuids = set()
                
                for edge in all_edges:
                    if edge["source_node_uuid"] == node["uuid"]:
                        related_edges.append({
                            "direction": "outgoing",
                            "edge_name": edge["name"],
                            "fact": edge["fact"],
                            "target_node_uuid": edge["target_node_uuid"],
                        })
                        related_node_uuids.add(edge["target_node_uuid"])
                    elif edge["target_node_uuid"] == node["uuid"]:
                        related_edges.append({
                            "direction": "incoming",
                            "edge_name": edge["name"],
                            "fact": edge["fact"],
                            "source_node_uuid": edge["source_node_uuid"],
                        })
                        related_node_uuids.add(edge["source_node_uuid"])
                
                entity.related_edges = related_edges
                
                # 獲取關聯節點的基本信息
                related_nodes = []
                for related_uuid in related_node_uuids:
                    if related_uuid in node_map:
                        related_node = node_map[related_uuid]
                        related_nodes.append({
                            "uuid": related_node["uuid"],
                            "name": related_node["name"],
                            "labels": related_node["labels"],
                            "summary": related_node.get("summary", ""),
                        })
                
                entity.related_nodes = related_nodes
            
            filtered_entities.append(entity)
        
        logger.info(f"篩選完成: 總節點 {total_count}, 符合條件 {len(filtered_entities)}, "
                   f"實體類型: {entity_types_found}")
        
        return FilteredEntities(
            entities=filtered_entities,
            entity_types=entity_types_found,
            total_count=total_count,
            filtered_count=len(filtered_entities),
        )
    
    def get_entity_with_context(
        self, 
        graph_id: str, 
        entity_uuid: str
    ) -> Optional[EntityNode]:
        """
        獲取單個實體及其完整上下文（邊和關聯節點，帶重試機制）
        
        Args:
            graph_id: 圖譜ID
            entity_uuid: 實體UUID
            
        Returns:
            EntityNode或None
        """
        try:
            if self.use_local:
                all_nodes = self.get_all_nodes(graph_id)
                node_map = {n["uuid"]: n for n in all_nodes}
                node = node_map.get(entity_uuid)
                if not node:
                    return None
                edges = [
                    edge for edge in self.get_all_edges(graph_id)
                    if edge.get("source_node_uuid") == entity_uuid or edge.get("target_node_uuid") == entity_uuid
                ]
                related_edges = []
                related_node_uuids = set()
                for edge in edges:
                    if edge.get("source_node_uuid") == entity_uuid:
                        related_edges.append({
                            "direction": "outgoing",
                            "edge_name": edge.get("name", ""),
                            "fact": edge.get("fact", ""),
                            "target_node_uuid": edge.get("target_node_uuid", ""),
                        })
                        related_node_uuids.add(edge.get("target_node_uuid", ""))
                    else:
                        related_edges.append({
                            "direction": "incoming",
                            "edge_name": edge.get("name", ""),
                            "fact": edge.get("fact", ""),
                            "source_node_uuid": edge.get("source_node_uuid", ""),
                        })
                        related_node_uuids.add(edge.get("source_node_uuid", ""))
                related_nodes = []
                for related_uuid in related_node_uuids:
                    related_node = node_map.get(related_uuid)
                    if related_node:
                        related_nodes.append({
                            "uuid": related_node.get("uuid", ""),
                            "name": related_node.get("name", ""),
                            "labels": related_node.get("labels", []),
                            "summary": related_node.get("summary", ""),
                        })
                return EntityNode(
                    uuid=node.get("uuid", ""),
                    name=node.get("name", ""),
                    labels=node.get("labels", []),
                    summary=node.get("summary", ""),
                    attributes=node.get("attributes", {}),
                    related_edges=related_edges,
                    related_nodes=related_nodes,
                )

            # 使用重試機制獲取節點
            node = self._call_with_retry(
                func=lambda: self.client.graph.node.get(uuid_=entity_uuid),
                operation_name=f"獲取節點詳情(uuid={entity_uuid[:8]}...)"
            )
            
            if not node:
                return None
            
            # 獲取節點的邊
            edges = self.get_node_edges(entity_uuid)
            
            # 獲取所有節點用於關聯查找
            all_nodes = self.get_all_nodes(graph_id)
            node_map = {n["uuid"]: n for n in all_nodes}
            
            # 處理相關邊和節點
            related_edges = []
            related_node_uuids = set()
            
            for edge in edges:
                if edge["source_node_uuid"] == entity_uuid:
                    related_edges.append({
                        "direction": "outgoing",
                        "edge_name": edge["name"],
                        "fact": edge["fact"],
                        "target_node_uuid": edge["target_node_uuid"],
                    })
                    related_node_uuids.add(edge["target_node_uuid"])
                else:
                    related_edges.append({
                        "direction": "incoming",
                        "edge_name": edge["name"],
                        "fact": edge["fact"],
                        "source_node_uuid": edge["source_node_uuid"],
                    })
                    related_node_uuids.add(edge["source_node_uuid"])
            
            # 獲取關聯節點信息
            related_nodes = []
            for related_uuid in related_node_uuids:
                if related_uuid in node_map:
                    related_node = node_map[related_uuid]
                    related_nodes.append({
                        "uuid": related_node["uuid"],
                        "name": related_node["name"],
                        "labels": related_node["labels"],
                        "summary": related_node.get("summary", ""),
                    })
            
            return EntityNode(
                uuid=getattr(node, 'uuid_', None) or getattr(node, 'uuid', ''),
                name=node.name or "",
                labels=node.labels or [],
                summary=node.summary or "",
                attributes=node.attributes or {},
                related_edges=related_edges,
                related_nodes=related_nodes,
            )
            
        except Exception as e:
            logger.error(f"獲取實體 {entity_uuid} 失敗: {str(e)}")
            return None
    
    def get_entities_by_type(
        self, 
        graph_id: str, 
        entity_type: str,
        enrich_with_edges: bool = True
    ) -> List[EntityNode]:
        """
        獲取指定類型的所有實體
        
        Args:
            graph_id: 圖譜ID
            entity_type: 實體類型（如 "Student", "PublicFigure" 等）
            enrich_with_edges: 是否獲取相關邊信息
            
        Returns:
            實體列表
        """
        result = self.filter_defined_entities(
            graph_id=graph_id,
            defined_entity_types=[entity_type],
            enrich_with_edges=enrich_with_edges
        )
        return result.entities
