"""
OASIS Agent Profile生成器
將Zep圖譜中的實體轉換為OASIS模擬平臺所需的Agent Profile格式

優化改進：
1. 調用Zep檢索功能二次豐富節點信息
2. 優化提示詞生成非常詳細的人設
3. 區分個人實體和抽象群體實體
"""

import json
import random
import time
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from datetime import datetime

from openai import OpenAI
try:
    from zep_cloud.client import Zep
except ImportError:
    Zep = None

from ..config import Config
from ..utils.logger import get_logger
from ..utils.locale import get_language_instruction, get_locale, set_locale, t
from .zep_entity_reader import EntityNode, ZepEntityReader

logger = get_logger('mirofish.oasis_profile')


@dataclass
class OasisAgentProfile:
    """OASIS Agent Profile數據結構"""
    # 通用字段
    user_id: int
    user_name: str
    name: str
    bio: str
    persona: str
    
    # 可選字段 - Reddit風格
    karma: int = 1000
    
    # 可選字段 - Twitter風格
    friend_count: int = 100
    follower_count: int = 150
    statuses_count: int = 500
    
    # 額外人設信息
    age: Optional[int] = None
    gender: Optional[str] = None
    mbti: Optional[str] = None
    country: Optional[str] = None
    profession: Optional[str] = None
    interested_topics: List[str] = field(default_factory=list)
    
    # 來源實體信息
    source_entity_uuid: Optional[str] = None
    source_entity_type: Optional[str] = None
    
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    
    def to_reddit_format(self) -> Dict[str, Any]:
        """轉換為Reddit平臺格式"""
        profile = {
            "user_id": self.user_id,
            "username": self.user_name,  # OASIS 庫要求字段名為 username（無下劃線）
            "name": self.name,
            "bio": self.bio,
            "persona": self.persona,
            "karma": self.karma,
            "created_at": self.created_at,
        }
        
        # 添加額外人設信息（如果有）
        if self.age:
            profile["age"] = self.age
        if self.gender:
            profile["gender"] = self.gender
        if self.mbti:
            profile["mbti"] = self.mbti
        if self.country:
            profile["country"] = self.country
        if self.profession:
            profile["profession"] = self.profession
        if self.interested_topics:
            profile["interested_topics"] = self.interested_topics
        
        return profile
    
    def to_twitter_format(self) -> Dict[str, Any]:
        """轉換為Twitter平臺格式"""
        profile = {
            "user_id": self.user_id,
            "username": self.user_name,  # OASIS 庫要求字段名為 username（無下劃線）
            "name": self.name,
            "bio": self.bio,
            "persona": self.persona,
            "friend_count": self.friend_count,
            "follower_count": self.follower_count,
            "statuses_count": self.statuses_count,
            "created_at": self.created_at,
        }
        
        # 添加額外人設信息
        if self.age:
            profile["age"] = self.age
        if self.gender:
            profile["gender"] = self.gender
        if self.mbti:
            profile["mbti"] = self.mbti
        if self.country:
            profile["country"] = self.country
        if self.profession:
            profile["profession"] = self.profession
        if self.interested_topics:
            profile["interested_topics"] = self.interested_topics
        
        return profile
    
    def to_dict(self) -> Dict[str, Any]:
        """轉換為完整字典格式"""
        return {
            "user_id": self.user_id,
            "user_name": self.user_name,
            "name": self.name,
            "bio": self.bio,
            "persona": self.persona,
            "karma": self.karma,
            "friend_count": self.friend_count,
            "follower_count": self.follower_count,
            "statuses_count": self.statuses_count,
            "age": self.age,
            "gender": self.gender,
            "mbti": self.mbti,
            "country": self.country,
            "profession": self.profession,
            "interested_topics": self.interested_topics,
            "source_entity_uuid": self.source_entity_uuid,
            "source_entity_type": self.source_entity_type,
            "created_at": self.created_at,
        }


class OasisProfileGenerator:
    """
    OASIS Profile生成器
    
    將Zep圖譜中的實體轉換為OASIS模擬所需的Agent Profile
    
    優化特性：
    1. 調用Zep圖譜檢索功能獲取更豐富的上下文
    2. 生成非常詳細的人設（包括基本信息、職業經歷、性格特徵、社交媒體行為等）
    3. 區分個人實體和抽象群體實體
    """
    
    # MBTI類型列表
    MBTI_TYPES = [
        "INTJ", "INTP", "ENTJ", "ENTP",
        "INFJ", "INFP", "ENFJ", "ENFP",
        "ISTJ", "ISFJ", "ESTJ", "ESFJ",
        "ISTP", "ISFP", "ESTP", "ESFP"
    ]
    
    # 常見國家列表
    COUNTRIES = [
        "China", "US", "UK", "Japan", "Germany", "France", 
        "Canada", "Australia", "Brazil", "India", "South Korea"
    ]
    
    # 個人類型實體（需要生成具體人設）
    INDIVIDUAL_ENTITY_TYPES = [
        "student", "alumni", "professor", "person", "publicfigure", 
        "expert", "faculty", "official", "journalist", "activist"
    ]
    
    # 群體/機構類型實體（需要生成群體代表人設）
    GROUP_ENTITY_TYPES = [
        "university", "governmentagency", "organization", "ngo", 
        "mediaoutlet", "company", "institution", "group", "community"
    ]
    
    def __init__(
        self, 
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
        zep_api_key: Optional[str] = None,
        graph_id: Optional[str] = None
    ):
        self.api_key = api_key or Config.LLM_API_KEY
        self.base_url = base_url or Config.LLM_BASE_URL
        self.model_name = model_name or Config.LLM_MODEL_NAME
        
        if not self.api_key:
            raise ValueError("LLM_API_KEY 未配置")
        
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )
        
        # Zep客戶端用於檢索豐富上下文
        self.zep_api_key = zep_api_key or Config.ZEP_API_KEY
        self.zep_client = None
        self.graph_id = graph_id
        
        if self.zep_api_key and Zep is not None and Config.MEMORY_BACKEND == "zep":
            try:
                self.zep_client = Zep(api_key=self.zep_api_key)
            except Exception as e:
                logger.warning(f"Zep客戶端初始化失敗: {e}")
    
    def generate_profile_from_entity(
        self, 
        entity: EntityNode, 
        user_id: int,
        use_llm: bool = True
    ) -> OasisAgentProfile:
        """
        從Zep實體生成OASIS Agent Profile
        
        Args:
            entity: Zep實體節點
            user_id: 用戶ID（用於OASIS）
            use_llm: 是否使用LLM生成詳細人設
            
        Returns:
            OasisAgentProfile
        """
        entity_type = entity.get_entity_type() or "Entity"
        
        # 基礎信息
        name = entity.name
        user_name = self._generate_username(name)
        
        # 構建上下文信息
        context = self._build_entity_context(entity)
        
        if use_llm:
            # 使用LLM生成詳細人設
            profile_data = self._generate_profile_with_llm(
                entity_name=name,
                entity_type=entity_type,
                entity_summary=entity.summary,
                entity_attributes=entity.attributes,
                context=context
            )
        else:
            # 使用規則生成基礎人設
            profile_data = self._generate_profile_rule_based(
                entity_name=name,
                entity_type=entity_type,
                entity_summary=entity.summary,
                entity_attributes=entity.attributes
            )
        
        return OasisAgentProfile(
            user_id=user_id,
            user_name=user_name,
            name=name,
            bio=profile_data.get("bio", f"{entity_type}: {name}"),
            persona=profile_data.get("persona", entity.summary or f"A {entity_type} named {name}."),
            karma=profile_data.get("karma", random.randint(500, 5000)),
            friend_count=profile_data.get("friend_count", random.randint(50, 500)),
            follower_count=profile_data.get("follower_count", random.randint(100, 1000)),
            statuses_count=profile_data.get("statuses_count", random.randint(100, 2000)),
            age=profile_data.get("age"),
            gender=profile_data.get("gender"),
            mbti=profile_data.get("mbti"),
            country=profile_data.get("country"),
            profession=profile_data.get("profession"),
            interested_topics=profile_data.get("interested_topics", []),
            source_entity_uuid=entity.uuid,
            source_entity_type=entity_type,
        )
    
    def _generate_username(self, name: str) -> str:
        """生成用戶名"""
        # 移除特殊字符，轉換為小寫
        username = name.lower().replace(" ", "_")
        username = ''.join(c for c in username if c.isalnum() or c == '_')
        
        # 添加隨機後綴避免重複
        suffix = random.randint(100, 999)
        return f"{username}_{suffix}"
    
    def _search_zep_for_entity(self, entity: EntityNode) -> Dict[str, Any]:
        """
        使用Zep圖譜混合搜索功能獲取實體相關的豐富信息
        
        Zep沒有內置混合搜索接口，需要分別搜索edges和nodes然後合併結果。
        使用並行請求同時搜索，提高效率。
        
        Args:
            entity: 實體節點對象
            
        Returns:
            包含facts, node_summaries, context的字典
        """
        import concurrent.futures
        
        if not self.zep_client:
            return {"facts": [], "node_summaries": [], "context": ""}
        
        entity_name = entity.name
        
        results = {
            "facts": [],
            "node_summaries": [],
            "context": ""
        }
        
        # 必須有graph_id才能進行搜索
        if not self.graph_id:
            logger.debug(f"跳過Zep檢索：未設置graph_id")
            return results
        
        comprehensive_query = t('progress.zepSearchQuery', name=entity_name)
        
        def search_edges():
            """搜索邊（事實/關係）- 帶重試機制"""
            max_retries = 3
            last_exception = None
            delay = 2.0
            
            for attempt in range(max_retries):
                try:
                    return self.zep_client.graph.search(
                        query=comprehensive_query,
                        graph_id=self.graph_id,
                        limit=30,
                        scope="edges",
                        reranker="rrf"
                    )
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.debug(f"Zep邊搜索第 {attempt + 1} 次失敗: {str(e)[:80]}, 重試中...")
                        time.sleep(delay)
                        delay *= 2
                    else:
                        logger.debug(f"Zep邊搜索在 {max_retries} 次嘗試後仍失敗: {e}")
            return None
        
        def search_nodes():
            """搜索節點（實體摘要）- 帶重試機制"""
            max_retries = 3
            last_exception = None
            delay = 2.0
            
            for attempt in range(max_retries):
                try:
                    return self.zep_client.graph.search(
                        query=comprehensive_query,
                        graph_id=self.graph_id,
                        limit=20,
                        scope="nodes",
                        reranker="rrf"
                    )
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.debug(f"Zep節點搜索第 {attempt + 1} 次失敗: {str(e)[:80]}, 重試中...")
                        time.sleep(delay)
                        delay *= 2
                    else:
                        logger.debug(f"Zep節點搜索在 {max_retries} 次嘗試後仍失敗: {e}")
            return None
        
        try:
            # 並行執行edges和nodes搜索
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                edge_future = executor.submit(search_edges)
                node_future = executor.submit(search_nodes)
                
                # 獲取結果
                edge_result = edge_future.result(timeout=30)
                node_result = node_future.result(timeout=30)
            
            # 處理邊搜索結果
            all_facts = set()
            if edge_result and hasattr(edge_result, 'edges') and edge_result.edges:
                for edge in edge_result.edges:
                    if hasattr(edge, 'fact') and edge.fact:
                        all_facts.add(edge.fact)
            results["facts"] = list(all_facts)
            
            # 處理節點搜索結果
            all_summaries = set()
            if node_result and hasattr(node_result, 'nodes') and node_result.nodes:
                for node in node_result.nodes:
                    if hasattr(node, 'summary') and node.summary:
                        all_summaries.add(node.summary)
                    if hasattr(node, 'name') and node.name and node.name != entity_name:
                        all_summaries.add(f"相關實體: {node.name}")
            results["node_summaries"] = list(all_summaries)
            
            # 構建綜合上下文
            context_parts = []
            if results["facts"]:
                context_parts.append("事實信息:\n" + "\n".join(f"- {f}" for f in results["facts"][:20]))
            if results["node_summaries"]:
                context_parts.append("相關實體:\n" + "\n".join(f"- {s}" for s in results["node_summaries"][:10]))
            results["context"] = "\n\n".join(context_parts)
            
            logger.info(f"Zep混合檢索完成: {entity_name}, 獲取 {len(results['facts'])} 條事實, {len(results['node_summaries'])} 個相關節點")
            
        except concurrent.futures.TimeoutError:
            logger.warning(f"Zep檢索超時 ({entity_name})")
        except Exception as e:
            logger.warning(f"Zep檢索失敗 ({entity_name}): {e}")
        
        return results
    
    def _build_entity_context(self, entity: EntityNode) -> str:
        """
        構建實體的完整上下文信息
        
        包括：
        1. 實體本身的邊信息（事實）
        2. 關聯節點的詳細信息
        3. Zep混合檢索到的豐富信息
        """
        context_parts = []
        
        # 1. 添加實體屬性信息
        if entity.attributes:
            attrs = []
            for key, value in entity.attributes.items():
                if value and str(value).strip():
                    attrs.append(f"- {key}: {value}")
            if attrs:
                context_parts.append("### 實體屬性\n" + "\n".join(attrs))
        
        # 2. 添加相關邊信息（事實/關係）
        existing_facts = set()
        if entity.related_edges:
            relationships = []
            for edge in entity.related_edges:  # 不限制數量
                fact = edge.get("fact", "")
                edge_name = edge.get("edge_name", "")
                direction = edge.get("direction", "")
                
                if fact:
                    relationships.append(f"- {fact}")
                    existing_facts.add(fact)
                elif edge_name:
                    if direction == "outgoing":
                        relationships.append(f"- {entity.name} --[{edge_name}]--> (相關實體)")
                    else:
                        relationships.append(f"- (相關實體) --[{edge_name}]--> {entity.name}")
            
            if relationships:
                context_parts.append("### 相關事實和關係\n" + "\n".join(relationships))
        
        # 3. 添加關聯節點的詳細信息
        if entity.related_nodes:
            related_info = []
            for node in entity.related_nodes:  # 不限制數量
                node_name = node.get("name", "")
                node_labels = node.get("labels", [])
                node_summary = node.get("summary", "")
                
                # 過濾掉默認標籤
                custom_labels = [l for l in node_labels if l not in ["Entity", "Node"]]
                label_str = f" ({', '.join(custom_labels)})" if custom_labels else ""
                
                if node_summary:
                    related_info.append(f"- **{node_name}**{label_str}: {node_summary}")
                else:
                    related_info.append(f"- **{node_name}**{label_str}")
            
            if related_info:
                context_parts.append("### 關聯實體信息\n" + "\n".join(related_info))
        
        # 4. 使用Zep混合檢索獲取更豐富的信息
        zep_results = self._search_zep_for_entity(entity)
        
        if zep_results.get("facts"):
            # 去重：排除已存在的事實
            new_facts = [f for f in zep_results["facts"] if f not in existing_facts]
            if new_facts:
                context_parts.append("### Zep檢索到的事實信息\n" + "\n".join(f"- {f}" for f in new_facts[:15]))
        
        if zep_results.get("node_summaries"):
            context_parts.append("### Zep檢索到的相關節點\n" + "\n".join(f"- {s}" for s in zep_results["node_summaries"][:10]))
        
        return "\n\n".join(context_parts)
    
    def _is_individual_entity(self, entity_type: str) -> bool:
        """判斷是否是個人類型實體"""
        return entity_type.lower() in self.INDIVIDUAL_ENTITY_TYPES
    
    def _is_group_entity(self, entity_type: str) -> bool:
        """判斷是否是群體/機構類型實體"""
        return entity_type.lower() in self.GROUP_ENTITY_TYPES
    
    def _generate_profile_with_llm(
        self,
        entity_name: str,
        entity_type: str,
        entity_summary: str,
        entity_attributes: Dict[str, Any],
        context: str
    ) -> Dict[str, Any]:
        """
        使用LLM生成非常詳細的人設
        
        根據實體類型區分：
        - 個人實體：生成具體的人物設定
        - 群體/機構實體：生成代表性賬號設定
        """
        
        is_individual = self._is_individual_entity(entity_type)
        
        if is_individual:
            prompt = self._build_individual_persona_prompt(
                entity_name, entity_type, entity_summary, entity_attributes, context
            )
        else:
            prompt = self._build_group_persona_prompt(
                entity_name, entity_type, entity_summary, entity_attributes, context
            )

        # 嘗試多次生成，直到成功或達到最大重試次數
        max_attempts = 3
        last_error = None
        
        for attempt in range(max_attempts):
            try:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": self._get_system_prompt(is_individual)},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.7 - (attempt * 0.1)  # 每次重試降低溫度
                    # 不設置max_tokens，讓LLM自由發揮
                )
                
                content = response.choices[0].message.content
                
                # 檢查是否被截斷（finish_reason不是'stop'）
                finish_reason = response.choices[0].finish_reason
                if finish_reason == 'length':
                    logger.warning(f"LLM輸出被截斷 (attempt {attempt+1}), 嘗試修復...")
                    content = self._fix_truncated_json(content)
                
                # 嘗試解析JSON
                try:
                    result = json.loads(content)
                    
                    # 驗證必需字段
                    if "bio" not in result or not result["bio"]:
                        result["bio"] = entity_summary[:200] if entity_summary else f"{entity_type}: {entity_name}"
                    if "persona" not in result or not result["persona"]:
                        result["persona"] = entity_summary or f"{entity_name}是一個{entity_type}。"
                    
                    return result
                    
                except json.JSONDecodeError as je:
                    logger.warning(f"JSON解析失敗 (attempt {attempt+1}): {str(je)[:80]}")
                    
                    # 嘗試修復JSON
                    result = self._try_fix_json(content, entity_name, entity_type, entity_summary)
                    if result.get("_fixed"):
                        del result["_fixed"]
                        return result
                    
                    last_error = je
                    
            except Exception as e:
                logger.warning(f"LLM調用失敗 (attempt {attempt+1}): {str(e)[:80]}")
                last_error = e
                import time
                time.sleep(1 * (attempt + 1))  # 指數退避
        
        logger.warning(f"LLM生成人設失敗（{max_attempts}次嘗試）: {last_error}, 使用規則生成")
        return self._generate_profile_rule_based(
            entity_name, entity_type, entity_summary, entity_attributes
        )
    
    def _fix_truncated_json(self, content: str) -> str:
        """修復被截斷的JSON（輸出被max_tokens限制截斷）"""
        import re
        
        # 如果JSON被截斷，嘗試閉合它
        content = content.strip()
        
        # 計算未閉合的括號
        open_braces = content.count('{') - content.count('}')
        open_brackets = content.count('[') - content.count(']')
        
        # 檢查是否有未閉合的字符串
        # 簡單檢查：如果最後一個引號後沒有逗號或閉合括號，可能是字符串被截斷
        if content and content[-1] not in '",}]':
            # 嘗試閉合字符串
            content += '"'
        
        # 閉合括號
        content += ']' * open_brackets
        content += '}' * open_braces
        
        return content
    
    def _try_fix_json(self, content: str, entity_name: str, entity_type: str, entity_summary: str = "") -> Dict[str, Any]:
        """嘗試修復損壞的JSON"""
        import re
        
        # 1. 首先嚐試修復被截斷的情況
        content = self._fix_truncated_json(content)
        
        # 2. 嘗試提取JSON部分
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            json_str = json_match.group()
            
            # 3. 處理字符串中的換行符問題
            # 找到所有字符串值並替換其中的換行符
            def fix_string_newlines(match):
                s = match.group(0)
                # 替換字符串內的實際換行符為空格
                s = s.replace('\n', ' ').replace('\r', ' ')
                # 替換多餘空格
                s = re.sub(r'\s+', ' ', s)
                return s
            
            # 匹配JSON字符串值
            json_str = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', fix_string_newlines, json_str)
            
            # 4. 嘗試解析
            try:
                result = json.loads(json_str)
                result["_fixed"] = True
                return result
            except json.JSONDecodeError as e:
                # 5. 如果還是失敗，嘗試更激進的修復
                try:
                    # 移除所有控制字符
                    json_str = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', json_str)
                    # 替換所有連續空白
                    json_str = re.sub(r'\s+', ' ', json_str)
                    result = json.loads(json_str)
                    result["_fixed"] = True
                    return result
                except:
                    pass
        
        # 6. 嘗試從內容中提取部分信息
        bio_match = re.search(r'"bio"\s*:\s*"([^"]*)"', content)
        persona_match = re.search(r'"persona"\s*:\s*"([^"]*)', content)  # 可能被截斷
        
        bio = bio_match.group(1) if bio_match else (entity_summary[:200] if entity_summary else f"{entity_type}: {entity_name}")
        persona = persona_match.group(1) if persona_match else (entity_summary or f"{entity_name}是一個{entity_type}。")
        
        # 如果提取到了有意義的內容，標記為已修復
        if bio_match or persona_match:
            logger.info(f"從損壞的JSON中提取了部分信息")
            return {
                "bio": bio,
                "persona": persona,
                "_fixed": True
            }
        
        # 7. 完全失敗，返回基礎結構
        logger.warning(f"JSON修復失敗，返回基礎結構")
        return {
            "bio": entity_summary[:200] if entity_summary else f"{entity_type}: {entity_name}",
            "persona": entity_summary or f"{entity_name}是一個{entity_type}。"
        }
    
    def _get_system_prompt(self, is_individual: bool) -> str:
        """獲取系統提示詞"""
        base_prompt = "你是社交媒體用戶畫像生成專家。生成詳細、真實的人設用於輿論模擬,最大程度還原已有現實情況。必須返回有效的JSON格式，所有字符串值不能包含未轉義的換行符。"
        return f"{base_prompt}\n\n{get_language_instruction()}"
    
    def _build_individual_persona_prompt(
        self,
        entity_name: str,
        entity_type: str,
        entity_summary: str,
        entity_attributes: Dict[str, Any],
        context: str
    ) -> str:
        """構建個人實體的詳細人設提示詞"""
        
        attrs_str = json.dumps(entity_attributes, ensure_ascii=False) if entity_attributes else "無"
        context_str = context[:3000] if context else "無額外上下文"
        
        return f"""為實體生成詳細的社交媒體用戶人設,最大程度還原已有現實情況。

實體名稱: {entity_name}
實體類型: {entity_type}
實體摘要: {entity_summary}
實體屬性: {attrs_str}

上下文信息:
{context_str}

請生成JSON，包含以下字段:

1. bio: 社交媒體簡介，200字
2. persona: 詳細人設描述（2000字的純文本），需包含:
   - 基本信息（年齡、職業、教育背景、所在地）
   - 人物背景（重要經歷、與事件的關聯、社會關係）
   - 性格特徵（MBTI類型、核心性格、情緒表達方式）
   - 社交媒體行為（發帖頻率、內容偏好、互動風格、語言特點）
   - 立場觀點（對話題的態度、可能被激怒/感動的內容）
   - 獨特特徵（口頭禪、特殊經歷、個人愛好）
   - 個人記憶（人設的重要部分，要介紹這個個體與事件的關聯，以及這個個體在事件中的已有動作與反應）
3. age: 年齡數字（必須是整數）
4. gender: 性別，必須是英文: "male" 或 "female"
5. mbti: MBTI類型（如INTJ、ENFP等）
6. country: 國家（使用中文，如"中國"）
7. profession: 職業
8. interested_topics: 感興趣話題數組

重要:
- 所有字段值必須是字符串或數字，不要使用換行符
- persona必須是一段連貫的文字描述
- {get_language_instruction()} (gender字段必須用英文male/female)
- 內容要與實體信息保持一致
- age必須是有效的整數，gender必須是"male"或"female"
"""

    def _build_group_persona_prompt(
        self,
        entity_name: str,
        entity_type: str,
        entity_summary: str,
        entity_attributes: Dict[str, Any],
        context: str
    ) -> str:
        """構建群體/機構實體的詳細人設提示詞"""
        
        attrs_str = json.dumps(entity_attributes, ensure_ascii=False) if entity_attributes else "無"
        context_str = context[:3000] if context else "無額外上下文"
        
        return f"""為機構/群體實體生成詳細的社交媒體賬號設定,最大程度還原已有現實情況。

實體名稱: {entity_name}
實體類型: {entity_type}
實體摘要: {entity_summary}
實體屬性: {attrs_str}

上下文信息:
{context_str}

請生成JSON，包含以下字段:

1. bio: 官方賬號簡介，200字，專業得體
2. persona: 詳細賬號設定描述（2000字的純文本），需包含:
   - 機構基本信息（正式名稱、機構性質、成立背景、主要職能）
   - 賬號定位（賬號類型、目標受眾、核心功能）
   - 發言風格（語言特點、常用表達、禁忌話題）
   - 發佈內容特點（內容類型、發佈頻率、活躍時間段）
   - 立場態度（對核心話題的官方立場、面對爭議的處理方式）
   - 特殊說明（代表的群體畫像、運營習慣）
   - 機構記憶（機構人設的重要部分，要介紹這個機構與事件的關聯，以及這個機構在事件中的已有動作與反應）
3. age: 固定填30（機構賬號的虛擬年齡）
4. gender: 固定填"other"（機構賬號使用other表示非個人）
5. mbti: MBTI類型，用於描述賬號風格，如ISTJ代表嚴謹保守
6. country: 國家（使用中文，如"中國"）
7. profession: 機構職能描述
8. interested_topics: 關注領域數組

重要:
- 所有字段值必須是字符串或數字，不允許null值
- persona必須是一段連貫的文字描述，不要使用換行符
- {get_language_instruction()} (gender字段必須用英文"other")
- age必須是整數30，gender必須是字符串"other"
- 機構賬號發言要符合其身份定位"""
    
    def _generate_profile_rule_based(
        self,
        entity_name: str,
        entity_type: str,
        entity_summary: str,
        entity_attributes: Dict[str, Any]
    ) -> Dict[str, Any]:
        """使用規則生成基礎人設"""
        
        # 根據實體類型生成不同的人設
        entity_type_lower = entity_type.lower()
        
        if entity_type_lower in ["student", "alumni"]:
            return {
                "bio": f"{entity_type} with interests in academics and social issues.",
                "persona": f"{entity_name} is a {entity_type.lower()} who is actively engaged in academic and social discussions. They enjoy sharing perspectives and connecting with peers.",
                "age": random.randint(18, 30),
                "gender": random.choice(["male", "female"]),
                "mbti": random.choice(self.MBTI_TYPES),
                "country": random.choice(self.COUNTRIES),
                "profession": "Student",
                "interested_topics": ["Education", "Social Issues", "Technology"],
            }
        
        elif entity_type_lower in ["publicfigure", "expert", "faculty"]:
            return {
                "bio": f"Expert and thought leader in their field.",
                "persona": f"{entity_name} is a recognized {entity_type.lower()} who shares insights and opinions on important matters. They are known for their expertise and influence in public discourse.",
                "age": random.randint(35, 60),
                "gender": random.choice(["male", "female"]),
                "mbti": random.choice(["ENTJ", "INTJ", "ENTP", "INTP"]),
                "country": random.choice(self.COUNTRIES),
                "profession": entity_attributes.get("occupation", "Expert"),
                "interested_topics": ["Politics", "Economics", "Culture & Society"],
            }
        
        elif entity_type_lower in ["mediaoutlet", "socialmediaplatform"]:
            return {
                "bio": f"Official account for {entity_name}. News and updates.",
                "persona": f"{entity_name} is a media entity that reports news and facilitates public discourse. The account shares timely updates and engages with the audience on current events.",
                "age": 30,  # 機構虛擬年齡
                "gender": "other",  # 機構使用other
                "mbti": "ISTJ",  # 機構風格：嚴謹保守
                "country": "中國",
                "profession": "Media",
                "interested_topics": ["General News", "Current Events", "Public Affairs"],
            }
        
        elif entity_type_lower in ["university", "governmentagency", "ngo", "organization"]:
            return {
                "bio": f"Official account of {entity_name}.",
                "persona": f"{entity_name} is an institutional entity that communicates official positions, announcements, and engages with stakeholders on relevant matters.",
                "age": 30,  # 機構虛擬年齡
                "gender": "other",  # 機構使用other
                "mbti": "ISTJ",  # 機構風格：嚴謹保守
                "country": "中國",
                "profession": entity_type,
                "interested_topics": ["Public Policy", "Community", "Official Announcements"],
            }
        
        else:
            # 默認人設
            return {
                "bio": entity_summary[:150] if entity_summary else f"{entity_type}: {entity_name}",
                "persona": entity_summary or f"{entity_name} is a {entity_type.lower()} participating in social discussions.",
                "age": random.randint(25, 50),
                "gender": random.choice(["male", "female"]),
                "mbti": random.choice(self.MBTI_TYPES),
                "country": random.choice(self.COUNTRIES),
                "profession": entity_type,
                "interested_topics": ["General", "Social Issues"],
            }
    
    def set_graph_id(self, graph_id: str):
        """設置圖譜ID用於Zep檢索"""
        self.graph_id = graph_id
    
    def generate_profiles_from_entities(
        self,
        entities: List[EntityNode],
        use_llm: bool = True,
        progress_callback: Optional[callable] = None,
        graph_id: Optional[str] = None,
        parallel_count: int = 5,
        realtime_output_path: Optional[str] = None,
        output_platform: str = "reddit"
    ) -> List[OasisAgentProfile]:
        """
        批量從實體生成Agent Profile（支持並行生成）
        
        Args:
            entities: 實體列表
            use_llm: 是否使用LLM生成詳細人設
            progress_callback: 進度回調函數 (current, total, message)
            graph_id: 圖譜ID，用於Zep檢索獲取更豐富上下文
            parallel_count: 並行生成數量，默認5
            realtime_output_path: 實時寫入的文件路徑（如果提供，每生成一個就寫入一次）
            output_platform: 輸出平臺格式 ("reddit" 或 "twitter")
            
        Returns:
            Agent Profile列表
        """
        import concurrent.futures
        from threading import Lock
        
        # 設置graph_id用於Zep檢索
        if graph_id:
            self.graph_id = graph_id
        
        total = len(entities)
        profiles = [None] * total  # 預分配列表保持順序
        completed_count = [0]  # 使用列表以便在閉包中修改
        lock = Lock()
        
        # 實時寫入文件的輔助函數
        def save_profiles_realtime():
            """實時保存已生成的 profiles 到文件"""
            if not realtime_output_path:
                return
            
            with lock:
                # 過濾出已生成的 profiles
                existing_profiles = [p for p in profiles if p is not None]
                if not existing_profiles:
                    return
                
                try:
                    if output_platform == "reddit":
                        # Reddit JSON 格式
                        profiles_data = [p.to_reddit_format() for p in existing_profiles]
                        with open(realtime_output_path, 'w', encoding='utf-8') as f:
                            json.dump(profiles_data, f, ensure_ascii=False, indent=2)
                    else:
                        # Twitter CSV 格式
                        import csv
                        profiles_data = [p.to_twitter_format() for p in existing_profiles]
                        if profiles_data:
                            fieldnames = list(profiles_data[0].keys())
                            with open(realtime_output_path, 'w', encoding='utf-8', newline='') as f:
                                writer = csv.DictWriter(f, fieldnames=fieldnames)
                                writer.writeheader()
                                writer.writerows(profiles_data)
                except Exception as e:
                    logger.warning(f"實時保存 profiles 失敗: {e}")
        
        # Capture locale before spawning thread pool workers
        current_locale = get_locale()

        def generate_single_profile(idx: int, entity: EntityNode) -> tuple:
            """生成單個profile的工作函數"""
            set_locale(current_locale)
            entity_type = entity.get_entity_type() or "Entity"
            
            try:
                profile = self.generate_profile_from_entity(
                    entity=entity,
                    user_id=idx,
                    use_llm=use_llm
                )
                
                # 實時輸出生成的人設到控制檯和日誌
                self._print_generated_profile(entity.name, entity_type, profile)
                
                return idx, profile, None
                
            except Exception as e:
                logger.error(f"生成實體 {entity.name} 的人設失敗: {str(e)}")
                # 創建一個基礎profile
                fallback_profile = OasisAgentProfile(
                    user_id=idx,
                    user_name=self._generate_username(entity.name),
                    name=entity.name,
                    bio=f"{entity_type}: {entity.name}",
                    persona=entity.summary or f"A participant in social discussions.",
                    source_entity_uuid=entity.uuid,
                    source_entity_type=entity_type,
                )
                return idx, fallback_profile, str(e)
        
        logger.info(f"開始並行生成 {total} 個Agent人設（並行數: {parallel_count}）...")
        print(f"\n{'='*60}")
        print(f"開始生成Agent人設 - 共 {total} 個實體，並行數: {parallel_count}")
        print(f"{'='*60}\n")
        
        # 使用線程池並行執行
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel_count) as executor:
            # 提交所有任務
            future_to_entity = {
                executor.submit(generate_single_profile, idx, entity): (idx, entity)
                for idx, entity in enumerate(entities)
            }
            
            # 收集結果
            for future in concurrent.futures.as_completed(future_to_entity):
                idx, entity = future_to_entity[future]
                entity_type = entity.get_entity_type() or "Entity"
                
                try:
                    result_idx, profile, error = future.result()
                    profiles[result_idx] = profile
                    
                    with lock:
                        completed_count[0] += 1
                        current = completed_count[0]
                    
                    # 實時寫入文件
                    save_profiles_realtime()
                    
                    if progress_callback:
                        progress_callback(
                            current, 
                            total, 
                            f"已完成 {current}/{total}: {entity.name}（{entity_type}）"
                        )
                    
                    if error:
                        logger.warning(f"[{current}/{total}] {entity.name} 使用備用人設: {error}")
                    else:
                        logger.info(f"[{current}/{total}] 成功生成人設: {entity.name} ({entity_type})")
                        
                except Exception as e:
                    logger.error(f"處理實體 {entity.name} 時發生異常: {str(e)}")
                    with lock:
                        completed_count[0] += 1
                    profiles[idx] = OasisAgentProfile(
                        user_id=idx,
                        user_name=self._generate_username(entity.name),
                        name=entity.name,
                        bio=f"{entity_type}: {entity.name}",
                        persona=entity.summary or "A participant in social discussions.",
                        source_entity_uuid=entity.uuid,
                        source_entity_type=entity_type,
                    )
                    # 實時寫入文件（即使是備用人設）
                    save_profiles_realtime()
        
        print(f"\n{'='*60}")
        print(f"人設生成完成！共生成 {len([p for p in profiles if p])} 個Agent")
        print(f"{'='*60}\n")
        
        return profiles
    
    def _print_generated_profile(self, entity_name: str, entity_type: str, profile: OasisAgentProfile):
        """實時輸出生成的人設到控制檯（完整內容，不截斷）"""
        separator = "-" * 70
        
        # 構建完整輸出內容（不截斷）
        topics_str = ', '.join(profile.interested_topics) if profile.interested_topics else '無'
        
        output_lines = [
            f"\n{separator}",
            t('progress.profileGenerated', name=entity_name, type=entity_type),
            f"{separator}",
            f"用戶名: {profile.user_name}",
            f"",
            f"【簡介】",
            f"{profile.bio}",
            f"",
            f"【詳細人設】",
            f"{profile.persona}",
            f"",
            f"【基本屬性】",
            f"年齡: {profile.age} | 性別: {profile.gender} | MBTI: {profile.mbti}",
            f"職業: {profile.profession} | 國家: {profile.country}",
            f"興趣話題: {topics_str}",
            separator
        ]
        
        output = "\n".join(output_lines)
        
        # 只輸出到控制檯（避免重複，logger不再輸出完整內容）
        print(output)
    
    def save_profiles(
        self,
        profiles: List[OasisAgentProfile],
        file_path: str,
        platform: str = "reddit"
    ):
        """
        保存Profile到文件（根據平臺選擇正確格式）
        
        OASIS平臺格式要求：
        - Twitter: CSV格式
        - Reddit: JSON格式
        
        Args:
            profiles: Profile列表
            file_path: 文件路徑
            platform: 平臺類型 ("reddit" 或 "twitter")
        """
        if platform == "twitter":
            self._save_twitter_csv(profiles, file_path)
        else:
            self._save_reddit_json(profiles, file_path)
    
    def _save_twitter_csv(self, profiles: List[OasisAgentProfile], file_path: str):
        """
        保存Twitter Profile為CSV格式（符合OASIS官方要求）
        
        OASIS Twitter要求的CSV字段：
        - user_id: 用戶ID（根據CSV順序從0開始）
        - name: 用戶真實姓名
        - username: 系統中的用戶名
        - user_char: 詳細人設描述（注入到LLM系統提示中，指導Agent行為）
        - description: 簡短的公開簡介（顯示在用戶資料頁面）
        
        user_char vs description 區別：
        - user_char: 內部使用，LLM系統提示，決定Agent如何思考和行動
        - description: 外部顯示，其他用戶可見的簡介
        """
        import csv
        
        # 確保文件擴展名是.csv
        if not file_path.endswith('.csv'):
            file_path = file_path.replace('.json', '.csv')
        
        with open(file_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            
            # 寫入OASIS要求的表頭
            headers = ['user_id', 'name', 'username', 'user_char', 'description']
            writer.writerow(headers)
            
            # 寫入數據行
            for idx, profile in enumerate(profiles):
                # user_char: 完整人設（bio + persona），用於LLM系統提示
                user_char = profile.bio
                if profile.persona and profile.persona != profile.bio:
                    user_char = f"{profile.bio} {profile.persona}"
                # 處理換行符（CSV中用空格替代）
                user_char = user_char.replace('\n', ' ').replace('\r', ' ')
                
                # description: 簡短簡介，用於外部顯示
                description = profile.bio.replace('\n', ' ').replace('\r', ' ')
                
                row = [
                    idx,                    # user_id: 從0開始的順序ID
                    profile.name,           # name: 真實姓名
                    profile.user_name,      # username: 用戶名
                    user_char,              # user_char: 完整人設（內部LLM使用）
                    description             # description: 簡短簡介（外部顯示）
                ]
                writer.writerow(row)
        
        logger.info(f"已保存 {len(profiles)} 個Twitter Profile到 {file_path} (OASIS CSV格式)")
    
    def _normalize_gender(self, gender: Optional[str]) -> str:
        """
        標準化gender字段為OASIS要求的英文格式
        
        OASIS要求: male, female, other
        """
        if not gender:
            return "other"
        
        gender_lower = gender.lower().strip()
        
        # 中文映射
        gender_map = {
            "男": "male",
            "女": "female",
            "機構": "other",
            "其他": "other",
            # 英文已有
            "male": "male",
            "female": "female",
            "other": "other",
        }
        
        return gender_map.get(gender_lower, "other")
    
    def _save_reddit_json(self, profiles: List[OasisAgentProfile], file_path: str):
        """
        保存Reddit Profile為JSON格式
        
        使用與 to_reddit_format() 一致的格式，確保 OASIS 能正確讀取。
        必須包含 user_id 字段，這是 OASIS agent_graph.get_agent() 匹配的關鍵！
        
        必需字段：
        - user_id: 用戶ID（整數，用於匹配 initial_posts 中的 poster_agent_id）
        - username: 用戶名
        - name: 顯示名稱
        - bio: 簡介
        - persona: 詳細人設
        - age: 年齡（整數）
        - gender: "male", "female", 或 "other"
        - mbti: MBTI類型
        - country: 國家
        """
        data = []
        for idx, profile in enumerate(profiles):
            # 使用與 to_reddit_format() 一致的格式
            item = {
                "user_id": profile.user_id if profile.user_id is not None else idx,  # 關鍵：必須包含 user_id
                "username": profile.user_name,
                "name": profile.name,
                "bio": profile.bio[:150] if profile.bio else f"{profile.name}",
                "persona": profile.persona or f"{profile.name} is a participant in social discussions.",
                "karma": profile.karma if profile.karma else 1000,
                "created_at": profile.created_at,
                # OASIS必需字段 - 確保都有默認值
                "age": profile.age if profile.age else 30,
                "gender": self._normalize_gender(profile.gender),
                "mbti": profile.mbti if profile.mbti else "ISTJ",
                "country": profile.country if profile.country else "中國",
            }
            
            # 可選字段
            if profile.profession:
                item["profession"] = profile.profession
            if profile.interested_topics:
                item["interested_topics"] = profile.interested_topics
            
            data.append(item)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        logger.info(f"已保存 {len(profiles)} 個Reddit Profile到 {file_path} (JSON格式，包含user_id字段)")
    
    # 保留舊方法名作為別名，保持向後兼容
    def save_profiles_to_json(
        self,
        profiles: List[OasisAgentProfile],
        file_path: str,
        platform: str = "reddit"
    ):
        """[已廢棄] 請使用 save_profiles() 方法"""
        logger.warning("save_profiles_to_json已廢棄，請使用save_profiles方法")
        self.save_profiles(profiles, file_path, platform)
