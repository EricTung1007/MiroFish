"""
模擬配置智能生成器
使用LLM根據模擬需求、文檔內容、圖譜信息自動生成細緻的模擬參數
實現全程自動化，無需人工設置參數

採用分步生成策略，避免一次性生成過長內容導致失敗：
1. 生成時間配置
2. 生成事件配置
3. 分批生成Agent配置
4. 生成平臺配置
"""

import json
import math
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field, asdict
from datetime import datetime

from openai import OpenAI

from ..config import Config
from ..utils.logger import get_logger
from ..utils.locale import get_language_instruction, t
from .zep_entity_reader import EntityNode, ZepEntityReader

logger = get_logger('mirofish.simulation_config')

# 中國作息時間配置（北京時間）
CHINA_TIMEZONE_CONFIG = {
    # 深夜時段（幾乎無人活動）
    "dead_hours": [0, 1, 2, 3, 4, 5],
    # 早間時段（逐漸醒來）
    "morning_hours": [6, 7, 8],
    # 工作時段
    "work_hours": [9, 10, 11, 12, 13, 14, 15, 16, 17, 18],
    # 晚間高峰（最活躍）
    "peak_hours": [19, 20, 21, 22],
    # 夜間時段（活躍度下降）
    "night_hours": [23],
    # 活躍度係數
    "activity_multipliers": {
        "dead": 0.05,      # 凌晨幾乎無人
        "morning": 0.4,    # 早間逐漸活躍
        "work": 0.7,       # 工作時段中等
        "peak": 1.5,       # 晚間高峰
        "night": 0.5       # 深夜下降
    }
}


@dataclass
class AgentActivityConfig:
    """單個Agent的活動配置"""
    agent_id: int
    entity_uuid: str
    entity_name: str
    entity_type: str
    
    # 活躍度配置 (0.0-1.0)
    activity_level: float = 0.5  # 整體活躍度
    
    # 發言頻率（每小時預期發言次數）
    posts_per_hour: float = 1.0
    comments_per_hour: float = 2.0
    
    # 活躍時間段（24小時制，0-23）
    active_hours: List[int] = field(default_factory=lambda: list(range(8, 23)))
    
    # 響應速度（對熱點事件的反應延遲，單位：模擬分鐘）
    response_delay_min: int = 5
    response_delay_max: int = 60
    
    # 情感傾向 (-1.0到1.0，負面到正面)
    sentiment_bias: float = 0.0
    
    # 立場（對特定話題的態度）
    stance: str = "neutral"  # supportive, opposing, neutral, observer
    
    # 影響力權重（決定其發言被其他Agent看到的概率）
    influence_weight: float = 1.0


@dataclass  
class TimeSimulationConfig:
    """時間模擬配置（基於中國人作息習慣）"""
    # 模擬總時長（模擬小時數）
    total_simulation_hours: int = 72  # 默認模擬72小時（3天）
    
    # 每輪代表的時間（模擬分鐘）- 默認60分鐘（1小時），加快時間流速
    minutes_per_round: int = 60
    
    # 每小時激活的Agent數量範圍
    agents_per_hour_min: int = 5
    agents_per_hour_max: int = 20
    
    # 高峰時段（晚間19-22點，中國人最活躍的時間）
    peak_hours: List[int] = field(default_factory=lambda: [19, 20, 21, 22])
    peak_activity_multiplier: float = 1.5
    
    # 低谷時段（凌晨0-5點，幾乎無人活動）
    off_peak_hours: List[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5])
    off_peak_activity_multiplier: float = 0.05  # 凌晨活躍度極低
    
    # 早間時段
    morning_hours: List[int] = field(default_factory=lambda: [6, 7, 8])
    morning_activity_multiplier: float = 0.4
    
    # 工作時段
    work_hours: List[int] = field(default_factory=lambda: [9, 10, 11, 12, 13, 14, 15, 16, 17, 18])
    work_activity_multiplier: float = 0.7


@dataclass
class EventConfig:
    """事件配置"""
    # 初始事件（模擬開始時的觸發事件）
    initial_posts: List[Dict[str, Any]] = field(default_factory=list)
    
    # 定時事件（在特定時間觸發的事件）
    scheduled_events: List[Dict[str, Any]] = field(default_factory=list)
    
    # 熱點話題關鍵詞
    hot_topics: List[str] = field(default_factory=list)
    
    # 輿論引導方向
    narrative_direction: str = ""


@dataclass
class PlatformConfig:
    """平臺特定配置"""
    platform: str  # twitter or reddit
    
    # 推薦算法權重
    recency_weight: float = 0.4  # 時間新鮮度
    popularity_weight: float = 0.3  # 熱度
    relevance_weight: float = 0.3  # 相關性
    
    # 病毒傳播閾值（達到多少互動後觸發擴散）
    viral_threshold: int = 10
    
    # 回聲室效應強度（相似觀點聚集程度）
    echo_chamber_strength: float = 0.5


@dataclass
class SimulationParameters:
    """完整的模擬參數配置"""
    # 基礎信息
    simulation_id: str
    project_id: str
    graph_id: str
    simulation_requirement: str
    
    # 時間配置
    time_config: TimeSimulationConfig = field(default_factory=TimeSimulationConfig)
    
    # Agent配置列表
    agent_configs: List[AgentActivityConfig] = field(default_factory=list)
    
    # 事件配置
    event_config: EventConfig = field(default_factory=EventConfig)
    
    # 平臺配置
    twitter_config: Optional[PlatformConfig] = None
    reddit_config: Optional[PlatformConfig] = None
    
    # LLM配置
    llm_model: str = ""
    llm_base_url: str = ""
    
    # 生成元數據
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    generation_reasoning: str = ""  # LLM的推理說明
    
    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典"""
        time_dict = asdict(self.time_config)
        return {
            "simulation_id": self.simulation_id,
            "project_id": self.project_id,
            "graph_id": self.graph_id,
            "simulation_requirement": self.simulation_requirement,
            "time_config": time_dict,
            "agent_configs": [asdict(a) for a in self.agent_configs],
            "event_config": asdict(self.event_config),
            "twitter_config": asdict(self.twitter_config) if self.twitter_config else None,
            "reddit_config": asdict(self.reddit_config) if self.reddit_config else None,
            "llm_model": self.llm_model,
            "llm_base_url": self.llm_base_url,
            "generated_at": self.generated_at,
            "generation_reasoning": self.generation_reasoning,
        }
    
    def to_json(self, indent: int = 2) -> str:
        """轉換為JSON字符串"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


class SimulationConfigGenerator:
    """
    模擬配置智能生成器
    
    使用LLM分析模擬需求、文檔內容、圖譜實體信息，
    自動生成最佳的模擬參數配置
    
    採用分步生成策略：
    1. 生成時間配置和事件配置（輕量級）
    2. 分批生成Agent配置（每批10-20個）
    3. 生成平臺配置
    """
    
    # 上下文最大字符數
    MAX_CONTEXT_LENGTH = 50000
    # 每批生成的Agent數量
    AGENTS_PER_BATCH = 15
    
    # 各步驟的上下文截斷長度（字符數）
    TIME_CONFIG_CONTEXT_LENGTH = 10000   # 時間配置
    EVENT_CONFIG_CONTEXT_LENGTH = 8000   # 事件配置
    ENTITY_SUMMARY_LENGTH = 300          # 實體摘要
    AGENT_SUMMARY_LENGTH = 300           # Agent配置中的實體摘要
    ENTITIES_PER_TYPE_DISPLAY = 20       # 每類實體顯示數量
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None
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
    
    def generate_config(
        self,
        simulation_id: str,
        project_id: str,
        graph_id: str,
        simulation_requirement: str,
        document_text: str,
        entities: List[EntityNode],
        enable_twitter: bool = True,
        enable_reddit: bool = True,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> SimulationParameters:
        """
        智能生成完整的模擬配置（分步生成）
        
        Args:
            simulation_id: 模擬ID
            project_id: 項目ID
            graph_id: 圖譜ID
            simulation_requirement: 模擬需求描述
            document_text: 原始文檔內容
            entities: 過濾後的實體列表
            enable_twitter: 是否啟用Twitter
            enable_reddit: 是否啟用Reddit
            progress_callback: 進度回調函數(current_step, total_steps, message)
            
        Returns:
            SimulationParameters: 完整的模擬參數
        """
        logger.info(f"開始智能生成模擬配置: simulation_id={simulation_id}, 實體數={len(entities)}")
        
        # 計算總步驟數
        num_batches = math.ceil(len(entities) / self.AGENTS_PER_BATCH)
        total_steps = 3 + num_batches  # 時間配置 + 事件配置 + N批Agent + 平臺配置
        current_step = 0
        
        def report_progress(step: int, message: str):
            nonlocal current_step
            current_step = step
            if progress_callback:
                progress_callback(step, total_steps, message)
            logger.info(f"[{step}/{total_steps}] {message}")
        
        # 1. 構建基礎上下文信息
        context = self._build_context(
            simulation_requirement=simulation_requirement,
            document_text=document_text,
            entities=entities
        )
        
        reasoning_parts = []
        
        # ========== 步驟1: 生成時間配置 ==========
        report_progress(1, t('progress.generatingTimeConfig'))
        num_entities = len(entities)
        time_config_result = self._generate_time_config(context, num_entities)
        time_config = self._parse_time_config(time_config_result, num_entities)
        reasoning_parts.append(f"{t('progress.timeConfigLabel')}: {time_config_result.get('reasoning', t('common.success'))}")
        
        # ========== 步驟2: 生成事件配置 ==========
        report_progress(2, t('progress.generatingEventConfig'))
        event_config_result = self._generate_event_config(context, simulation_requirement, entities)
        event_config = self._parse_event_config(event_config_result)
        reasoning_parts.append(f"{t('progress.eventConfigLabel')}: {event_config_result.get('reasoning', t('common.success'))}")
        
        # ========== 步驟3-N: 分批生成Agent配置 ==========
        all_agent_configs = []
        for batch_idx in range(num_batches):
            start_idx = batch_idx * self.AGENTS_PER_BATCH
            end_idx = min(start_idx + self.AGENTS_PER_BATCH, len(entities))
            batch_entities = entities[start_idx:end_idx]
            
            report_progress(
                3 + batch_idx,
                t('progress.generatingAgentConfig', start=start_idx + 1, end=end_idx, total=len(entities))
            )
            
            batch_configs = self._generate_agent_configs_batch(
                context=context,
                entities=batch_entities,
                start_idx=start_idx,
                simulation_requirement=simulation_requirement
            )
            all_agent_configs.extend(batch_configs)
        
        reasoning_parts.append(t('progress.agentConfigResult', count=len(all_agent_configs)))
        
        # ========== 為初始帖子分配發布者 Agent ==========
        logger.info("為初始帖子分配合適的發佈者 Agent...")
        event_config = self._assign_initial_post_agents(event_config, all_agent_configs)
        assigned_count = len([p for p in event_config.initial_posts if p.get("poster_agent_id") is not None])
        reasoning_parts.append(t('progress.postAssignResult', count=assigned_count))
        
        # ========== 最後一步: 生成平臺配置 ==========
        report_progress(total_steps, t('progress.generatingPlatformConfig'))
        twitter_config = None
        reddit_config = None
        
        if enable_twitter:
            twitter_config = PlatformConfig(
                platform="twitter",
                recency_weight=0.4,
                popularity_weight=0.3,
                relevance_weight=0.3,
                viral_threshold=10,
                echo_chamber_strength=0.5
            )
        
        if enable_reddit:
            reddit_config = PlatformConfig(
                platform="reddit",
                recency_weight=0.3,
                popularity_weight=0.4,
                relevance_weight=0.3,
                viral_threshold=15,
                echo_chamber_strength=0.6
            )
        
        # 構建最終參數
        params = SimulationParameters(
            simulation_id=simulation_id,
            project_id=project_id,
            graph_id=graph_id,
            simulation_requirement=simulation_requirement,
            time_config=time_config,
            agent_configs=all_agent_configs,
            event_config=event_config,
            twitter_config=twitter_config,
            reddit_config=reddit_config,
            llm_model=self.model_name,
            llm_base_url=self.base_url,
            generation_reasoning=" | ".join(reasoning_parts)
        )
        
        logger.info(f"模擬配置生成完成: {len(params.agent_configs)} 個Agent配置")
        
        return params
    
    def _build_context(
        self,
        simulation_requirement: str,
        document_text: str,
        entities: List[EntityNode]
    ) -> str:
        """構建LLM上下文，截斷到最大長度"""
        
        # 實體摘要
        entity_summary = self._summarize_entities(entities)
        
        # 構建上下文
        context_parts = [
            f"## 模擬需求\n{simulation_requirement}",
            f"\n## 實體信息 ({len(entities)}個)\n{entity_summary}",
        ]
        
        current_length = sum(len(p) for p in context_parts)
        remaining_length = self.MAX_CONTEXT_LENGTH - current_length - 500  # 留500字符餘量
        
        if remaining_length > 0 and document_text:
            doc_text = document_text[:remaining_length]
            if len(document_text) > remaining_length:
                doc_text += "\n...(文檔已截斷)"
            context_parts.append(f"\n## 原始文檔內容\n{doc_text}")
        
        return "\n".join(context_parts)
    
    def _summarize_entities(self, entities: List[EntityNode]) -> str:
        """生成實體摘要"""
        lines = []
        
        # 按類型分組
        by_type: Dict[str, List[EntityNode]] = {}
        for e in entities:
            t = e.get_entity_type() or "Unknown"
            if t not in by_type:
                by_type[t] = []
            by_type[t].append(e)
        
        for entity_type, type_entities in by_type.items():
            lines.append(f"\n### {entity_type} ({len(type_entities)}個)")
            # 使用配置的顯示數量和摘要長度
            display_count = self.ENTITIES_PER_TYPE_DISPLAY
            summary_len = self.ENTITY_SUMMARY_LENGTH
            for e in type_entities[:display_count]:
                summary_preview = (e.summary[:summary_len] + "...") if len(e.summary) > summary_len else e.summary
                lines.append(f"- {e.name}: {summary_preview}")
            if len(type_entities) > display_count:
                lines.append(f"  ... 還有 {len(type_entities) - display_count} 個")
        
        return "\n".join(lines)
    
    def _call_llm_with_retry(self, prompt: str, system_prompt: str) -> Dict[str, Any]:
        """帶重試的LLM調用，包含JSON修復邏輯"""
        import re
        
        max_attempts = 3
        last_error = None
        
        for attempt in range(max_attempts):
            try:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.7 - (attempt * 0.1)  # 每次重試降低溫度
                    # 不設置max_tokens，讓LLM自由發揮
                )
                
                content = response.choices[0].message.content
                finish_reason = response.choices[0].finish_reason
                
                # 檢查是否被截斷
                if finish_reason == 'length':
                    logger.warning(f"LLM輸出被截斷 (attempt {attempt+1})")
                    content = self._fix_truncated_json(content)
                
                # 嘗試解析JSON
                try:
                    return json.loads(content)
                except json.JSONDecodeError as e:
                    logger.warning(f"JSON解析失敗 (attempt {attempt+1}): {str(e)[:80]}")
                    
                    # 嘗試修復JSON
                    fixed = self._try_fix_config_json(content)
                    if fixed:
                        return fixed
                    
                    last_error = e
                    
            except Exception as e:
                logger.warning(f"LLM調用失敗 (attempt {attempt+1}): {str(e)[:80]}")
                last_error = e
                import time
                time.sleep(2 * (attempt + 1))
        
        raise last_error or Exception("LLM調用失敗")
    
    def _fix_truncated_json(self, content: str) -> str:
        """修復被截斷的JSON"""
        content = content.strip()
        
        # 計算未閉合的括號
        open_braces = content.count('{') - content.count('}')
        open_brackets = content.count('[') - content.count(']')
        
        # 檢查是否有未閉合的字符串
        if content and content[-1] not in '",}]':
            content += '"'
        
        # 閉合括號
        content += ']' * open_brackets
        content += '}' * open_braces
        
        return content
    
    def _try_fix_config_json(self, content: str) -> Optional[Dict[str, Any]]:
        """嘗試修復配置JSON"""
        import re
        
        # 修復被截斷的情況
        content = self._fix_truncated_json(content)
        
        # 提取JSON部分
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            json_str = json_match.group()
            
            # 移除字符串中的換行符
            def fix_string(match):
                s = match.group(0)
                s = s.replace('\n', ' ').replace('\r', ' ')
                s = re.sub(r'\s+', ' ', s)
                return s
            
            json_str = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', fix_string, json_str)
            
            try:
                return json.loads(json_str)
            except:
                # 嘗試移除所有控制字符
                json_str = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', json_str)
                json_str = re.sub(r'\s+', ' ', json_str)
                try:
                    return json.loads(json_str)
                except:
                    pass
        
        return None
    
    def _generate_time_config(self, context: str, num_entities: int) -> Dict[str, Any]:
        """生成時間配置"""
        # 使用配置的上下文截斷長度
        context_truncated = context[:self.TIME_CONFIG_CONTEXT_LENGTH]
        
        # 計算最大允許值（80%的agent數）
        max_agents_allowed = max(1, int(num_entities * 0.9))
        
        prompt = f"""基於以下模擬需求，生成時間模擬配置。

{context_truncated}

## 任務
請生成時間配置JSON。

### 基本原則（僅供參考，需根據具體事件和參與群體靈活調整）：
- 請根據模擬場景推斷目標用戶群體所在時區和作息習慣，以下為東八區(UTC+8)的參考示例
- 凌晨0-5點幾乎無人活動（活躍度係數0.05）
- 早上6-8點逐漸活躍（活躍度係數0.4）
- 工作時間9-18點中等活躍（活躍度係數0.7）
- 晚間19-22點是高峰期（活躍度係數1.5）
- 23點後活躍度下降（活躍度係數0.5）
- 一般規律：凌晨低活躍、早間漸增、工作時段中等、晚間高峰
- **重要**：以下示例值僅供參考，你需要根據事件性質、參與群體特點來調整具體時段
  - 例如：學生群體高峰可能是21-23點；媒體全天活躍；官方機構只在工作時間
  - 例如：突發熱點可能導致深夜也有討論，off_peak_hours 可適當縮短

### 返回JSON格式（不要markdown）

示例：
{{
    "total_simulation_hours": 72,
    "minutes_per_round": 60,
    "agents_per_hour_min": 5,
    "agents_per_hour_max": 50,
    "peak_hours": [19, 20, 21, 22],
    "off_peak_hours": [0, 1, 2, 3, 4, 5],
    "morning_hours": [6, 7, 8],
    "work_hours": [9, 10, 11, 12, 13, 14, 15, 16, 17, 18],
    "reasoning": "針對該事件的時間配置說明"
}}

字段說明：
- total_simulation_hours (int): 模擬總時長，24-168小時，突發事件短、持續話題長
- minutes_per_round (int): 每輪時長，30-120分鐘，建議60分鐘
- agents_per_hour_min (int): 每小時最少激活Agent數（取值範圍: 1-{max_agents_allowed}）
- agents_per_hour_max (int): 每小時最多激活Agent數（取值範圍: 1-{max_agents_allowed}）
- peak_hours (int數組): 高峰時段，根據事件參與群體調整
- off_peak_hours (int數組): 低谷時段，通常深夜凌晨
- morning_hours (int數組): 早間時段
- work_hours (int數組): 工作時段
- reasoning (string): 簡要說明為什麼這樣配置"""

        system_prompt = "你是社交媒體模擬專家。返回純JSON格式，時間配置需符合模擬場景中目標用戶群體的作息習慣。"
        system_prompt = f"{system_prompt}\n\n{get_language_instruction()}"

        try:
            return self._call_llm_with_retry(prompt, system_prompt)
        except Exception as e:
            logger.warning(f"時間配置LLM生成失敗: {e}, 使用默認配置")
            return self._get_default_time_config(num_entities)
    
    def _get_default_time_config(self, num_entities: int) -> Dict[str, Any]:
        """獲取默認時間配置（中國人作息）"""
        return {
            "total_simulation_hours": 72,
            "minutes_per_round": 60,  # 每輪1小時，加快時間流速
            "agents_per_hour_min": max(1, num_entities // 15),
            "agents_per_hour_max": max(5, num_entities // 5),
            "peak_hours": [19, 20, 21, 22],
            "off_peak_hours": [0, 1, 2, 3, 4, 5],
            "morning_hours": [6, 7, 8],
            "work_hours": [9, 10, 11, 12, 13, 14, 15, 16, 17, 18],
            "reasoning": "使用默認中國人作息配置（每輪1小時）"
        }
    
    def _parse_time_config(self, result: Dict[str, Any], num_entities: int) -> TimeSimulationConfig:
        """解析時間配置結果，並驗證agents_per_hour值不超過總agent數"""
        # 獲取原始值
        agents_per_hour_min = result.get("agents_per_hour_min", max(1, num_entities // 15))
        agents_per_hour_max = result.get("agents_per_hour_max", max(5, num_entities // 5))
        
        # 驗證並修正：確保不超過總agent數
        if agents_per_hour_min > num_entities:
            logger.warning(f"agents_per_hour_min ({agents_per_hour_min}) 超過總Agent數 ({num_entities})，已修正")
            agents_per_hour_min = max(1, num_entities // 10)
        
        if agents_per_hour_max > num_entities:
            logger.warning(f"agents_per_hour_max ({agents_per_hour_max}) 超過總Agent數 ({num_entities})，已修正")
            agents_per_hour_max = max(agents_per_hour_min + 1, num_entities // 2)
        
        # 確保 min < max
        if agents_per_hour_min >= agents_per_hour_max:
            agents_per_hour_min = max(1, agents_per_hour_max // 2)
            logger.warning(f"agents_per_hour_min >= max，已修正為 {agents_per_hour_min}")
        
        return TimeSimulationConfig(
            total_simulation_hours=result.get("total_simulation_hours", 72),
            minutes_per_round=result.get("minutes_per_round", 60),  # 默認每輪1小時
            agents_per_hour_min=agents_per_hour_min,
            agents_per_hour_max=agents_per_hour_max,
            peak_hours=result.get("peak_hours", [19, 20, 21, 22]),
            off_peak_hours=result.get("off_peak_hours", [0, 1, 2, 3, 4, 5]),
            off_peak_activity_multiplier=0.05,  # 凌晨幾乎無人
            morning_hours=result.get("morning_hours", [6, 7, 8]),
            morning_activity_multiplier=0.4,
            work_hours=result.get("work_hours", list(range(9, 19))),
            work_activity_multiplier=0.7,
            peak_activity_multiplier=1.5
        )
    
    def _generate_event_config(
        self, 
        context: str, 
        simulation_requirement: str,
        entities: List[EntityNode]
    ) -> Dict[str, Any]:
        """生成事件配置"""
        
        # 獲取可用的實體類型列表，供 LLM 參考
        entity_types_available = list(set(
            e.get_entity_type() or "Unknown" for e in entities
        ))
        
        # 為每種類型列出代表性實體名稱
        type_examples = {}
        for e in entities:
            etype = e.get_entity_type() or "Unknown"
            if etype not in type_examples:
                type_examples[etype] = []
            if len(type_examples[etype]) < 3:
                type_examples[etype].append(e.name)
        
        type_info = "\n".join([
            f"- {t}: {', '.join(examples)}" 
            for t, examples in type_examples.items()
        ])
        
        # 使用配置的上下文截斷長度
        context_truncated = context[:self.EVENT_CONFIG_CONTEXT_LENGTH]
        
        prompt = f"""基於以下模擬需求，生成事件配置。

模擬需求: {simulation_requirement}

{context_truncated}

## 可用實體類型及示例
{type_info}

## 任務
請生成事件配置JSON：
- 提取熱點話題關鍵詞
- 描述輿論發展方向
- 設計初始帖子內容，**每個帖子必須指定 poster_type（發佈者類型）**

**重要**: poster_type 必須從上面的"可用實體類型"中選擇，這樣初始帖子才能分配給合適的 Agent 發佈。
例如：官方聲明應由 Official/University 類型發佈，新聞由 MediaOutlet 發佈，學生觀點由 Student 發佈。

返回JSON格式（不要markdown）：
{{
    "hot_topics": ["關鍵詞1", "關鍵詞2", ...],
    "narrative_direction": "<輿論發展方向描述>",
    "initial_posts": [
        {{"content": "帖子內容", "poster_type": "實體類型（必須從可用類型中選擇）"}},
        ...
    ],
    "reasoning": "<簡要說明>"
}}"""

        system_prompt = "你是輿論分析專家。返回純JSON格式。注意 poster_type 必須精確匹配可用實體類型。"
        system_prompt = f"{system_prompt}\n\n{get_language_instruction()}\nIMPORTANT: The 'poster_type' field value MUST be in English PascalCase exactly matching the available entity types. Only 'content', 'narrative_direction', 'hot_topics' and 'reasoning' fields should use the specified language."

        try:
            return self._call_llm_with_retry(prompt, system_prompt)
        except Exception as e:
            logger.warning(f"事件配置LLM生成失敗: {e}, 使用默認配置")
            return {
                "hot_topics": [],
                "narrative_direction": "",
                "initial_posts": [],
                "reasoning": "使用默認配置"
            }
    
    def _parse_event_config(self, result: Dict[str, Any]) -> EventConfig:
        """解析事件配置結果"""
        return EventConfig(
            initial_posts=result.get("initial_posts", []),
            scheduled_events=[],
            hot_topics=result.get("hot_topics", []),
            narrative_direction=result.get("narrative_direction", "")
        )
    
    def _assign_initial_post_agents(
        self,
        event_config: EventConfig,
        agent_configs: List[AgentActivityConfig]
    ) -> EventConfig:
        """
        為初始帖子分配合適的發佈者 Agent
        
        根據每個帖子的 poster_type 匹配最合適的 agent_id
        """
        if not event_config.initial_posts:
            return event_config
        
        # 按實體類型建立 agent 索引
        agents_by_type: Dict[str, List[AgentActivityConfig]] = {}
        for agent in agent_configs:
            etype = agent.entity_type.lower()
            if etype not in agents_by_type:
                agents_by_type[etype] = []
            agents_by_type[etype].append(agent)
        
        # 類型映射表（處理 LLM 可能輸出的不同格式）
        type_aliases = {
            "official": ["official", "university", "governmentagency", "government"],
            "university": ["university", "official"],
            "mediaoutlet": ["mediaoutlet", "media"],
            "student": ["student", "person"],
            "professor": ["professor", "expert", "teacher"],
            "alumni": ["alumni", "person"],
            "organization": ["organization", "ngo", "company", "group"],
            "person": ["person", "student", "alumni"],
        }
        
        # 記錄每種類型已使用的 agent 索引，避免重複使用同一個 agent
        used_indices: Dict[str, int] = {}
        
        updated_posts = []
        for post in event_config.initial_posts:
            poster_type = post.get("poster_type", "").lower()
            content = post.get("content", "")
            
            # 嘗試找到匹配的 agent
            matched_agent_id = None
            
            # 1. 直接匹配
            if poster_type in agents_by_type:
                agents = agents_by_type[poster_type]
                idx = used_indices.get(poster_type, 0) % len(agents)
                matched_agent_id = agents[idx].agent_id
                used_indices[poster_type] = idx + 1
            else:
                # 2. 使用別名匹配
                for alias_key, aliases in type_aliases.items():
                    if poster_type in aliases or alias_key == poster_type:
                        for alias in aliases:
                            if alias in agents_by_type:
                                agents = agents_by_type[alias]
                                idx = used_indices.get(alias, 0) % len(agents)
                                matched_agent_id = agents[idx].agent_id
                                used_indices[alias] = idx + 1
                                break
                    if matched_agent_id is not None:
                        break
            
            # 3. 如果仍未找到，使用影響力最高的 agent
            if matched_agent_id is None:
                logger.warning(f"未找到類型 '{poster_type}' 的匹配 Agent，使用影響力最高的 Agent")
                if agent_configs:
                    # 按影響力排序，選擇影響力最高的
                    sorted_agents = sorted(agent_configs, key=lambda a: a.influence_weight, reverse=True)
                    matched_agent_id = sorted_agents[0].agent_id
                else:
                    matched_agent_id = 0
            
            updated_posts.append({
                "content": content,
                "poster_type": post.get("poster_type", "Unknown"),
                "poster_agent_id": matched_agent_id
            })
            
            logger.info(f"初始帖子分配: poster_type='{poster_type}' -> agent_id={matched_agent_id}")
        
        event_config.initial_posts = updated_posts
        return event_config
    
    def _generate_agent_configs_batch(
        self,
        context: str,
        entities: List[EntityNode],
        start_idx: int,
        simulation_requirement: str
    ) -> List[AgentActivityConfig]:
        """分批生成Agent配置"""
        
        # 構建實體信息（使用配置的摘要長度）
        entity_list = []
        summary_len = self.AGENT_SUMMARY_LENGTH
        for i, e in enumerate(entities):
            entity_list.append({
                "agent_id": start_idx + i,
                "entity_name": e.name,
                "entity_type": e.get_entity_type() or "Unknown",
                "summary": e.summary[:summary_len] if e.summary else ""
            })
        
        prompt = f"""基於以下信息，為每個實體生成社交媒體活動配置。

模擬需求: {simulation_requirement}

## 實體列表
```json
{json.dumps(entity_list, ensure_ascii=False, indent=2)}
```

## 任務
為每個實體生成活動配置，注意：
- **時間符合目標用戶群體作息**：以下為參考（東八區），請根據模擬場景調整
- **官方機構**（University/GovernmentAgency）：活躍度低(0.1-0.3)，工作時間(9-17)活動，響應慢(60-240分鐘)，影響力高(2.5-3.0)
- **媒體**（MediaOutlet）：活躍度中(0.4-0.6)，全天活動(8-23)，響應快(5-30分鐘)，影響力高(2.0-2.5)
- **個人**（Student/Person/Alumni）：活躍度高(0.6-0.9)，主要晚間活動(18-23)，響應快(1-15分鐘)，影響力低(0.8-1.2)
- **公眾人物/專家**：活躍度中(0.4-0.6)，影響力中高(1.5-2.0)

返回JSON格式（不要markdown）：
{{
    "agent_configs": [
        {{
            "agent_id": <必須與輸入一致>,
            "activity_level": <0.0-1.0>,
            "posts_per_hour": <發帖頻率>,
            "comments_per_hour": <評論頻率>,
            "active_hours": [<活躍小時列表，考慮中國人作息>],
            "response_delay_min": <最小響應延遲分鐘>,
            "response_delay_max": <最大響應延遲分鐘>,
            "sentiment_bias": <-1.0到1.0>,
            "stance": "<supportive/opposing/neutral/observer>",
            "influence_weight": <影響力權重>
        }},
        ...
    ]
}}"""

        system_prompt = "你是社交媒體行為分析專家。返回純JSON，配置需符合模擬場景中目標用戶群體的作息習慣。"
        system_prompt = f"{system_prompt}\n\n{get_language_instruction()}\nIMPORTANT: The 'stance' field value MUST be one of the English strings: 'supportive', 'opposing', 'neutral', 'observer'. All JSON field names and numeric values must remain unchanged. Only natural language text fields should use the specified language."

        try:
            result = self._call_llm_with_retry(prompt, system_prompt)
            llm_configs = {cfg["agent_id"]: cfg for cfg in result.get("agent_configs", [])}
        except Exception as e:
            logger.warning(f"Agent配置批次LLM生成失敗: {e}, 使用規則生成")
            llm_configs = {}
        
        # 構建AgentActivityConfig對象
        configs = []
        for i, entity in enumerate(entities):
            agent_id = start_idx + i
            cfg = llm_configs.get(agent_id, {})
            
            # 如果LLM沒有生成，使用規則生成
            if not cfg:
                cfg = self._generate_agent_config_by_rule(entity)
            
            config = AgentActivityConfig(
                agent_id=agent_id,
                entity_uuid=entity.uuid,
                entity_name=entity.name,
                entity_type=entity.get_entity_type() or "Unknown",
                activity_level=cfg.get("activity_level", 0.5),
                posts_per_hour=cfg.get("posts_per_hour", 0.5),
                comments_per_hour=cfg.get("comments_per_hour", 1.0),
                active_hours=cfg.get("active_hours", list(range(9, 23))),
                response_delay_min=cfg.get("response_delay_min", 5),
                response_delay_max=cfg.get("response_delay_max", 60),
                sentiment_bias=cfg.get("sentiment_bias", 0.0),
                stance=cfg.get("stance", "neutral"),
                influence_weight=cfg.get("influence_weight", 1.0)
            )
            configs.append(config)
        
        return configs
    
    def _generate_agent_config_by_rule(self, entity: EntityNode) -> Dict[str, Any]:
        """基於規則生成單個Agent配置（中國人作息）"""
        entity_type = (entity.get_entity_type() or "Unknown").lower()
        
        if entity_type in ["university", "governmentagency", "ngo"]:
            # 官方機構：工作時間活動，低頻率，高影響力
            return {
                "activity_level": 0.2,
                "posts_per_hour": 0.1,
                "comments_per_hour": 0.05,
                "active_hours": list(range(9, 18)),  # 9:00-17:59
                "response_delay_min": 60,
                "response_delay_max": 240,
                "sentiment_bias": 0.0,
                "stance": "neutral",
                "influence_weight": 3.0
            }
        elif entity_type in ["mediaoutlet"]:
            # 媒體：全天活動，中等頻率，高影響力
            return {
                "activity_level": 0.5,
                "posts_per_hour": 0.8,
                "comments_per_hour": 0.3,
                "active_hours": list(range(7, 24)),  # 7:00-23:59
                "response_delay_min": 5,
                "response_delay_max": 30,
                "sentiment_bias": 0.0,
                "stance": "observer",
                "influence_weight": 2.5
            }
        elif entity_type in ["professor", "expert", "official"]:
            # 專家/教授：工作+晚間活動，中等頻率
            return {
                "activity_level": 0.4,
                "posts_per_hour": 0.3,
                "comments_per_hour": 0.5,
                "active_hours": list(range(8, 22)),  # 8:00-21:59
                "response_delay_min": 15,
                "response_delay_max": 90,
                "sentiment_bias": 0.0,
                "stance": "neutral",
                "influence_weight": 2.0
            }
        elif entity_type in ["student"]:
            # 學生：晚間為主，高頻率
            return {
                "activity_level": 0.8,
                "posts_per_hour": 0.6,
                "comments_per_hour": 1.5,
                "active_hours": [8, 9, 10, 11, 12, 13, 18, 19, 20, 21, 22, 23],  # 上午+晚間
                "response_delay_min": 1,
                "response_delay_max": 15,
                "sentiment_bias": 0.0,
                "stance": "neutral",
                "influence_weight": 0.8
            }
        elif entity_type in ["alumni"]:
            # 校友：晚間為主
            return {
                "activity_level": 0.6,
                "posts_per_hour": 0.4,
                "comments_per_hour": 0.8,
                "active_hours": [12, 13, 19, 20, 21, 22, 23],  # 午休+晚間
                "response_delay_min": 5,
                "response_delay_max": 30,
                "sentiment_bias": 0.0,
                "stance": "neutral",
                "influence_weight": 1.0
            }
        else:
            # 普通人：晚間高峰
            return {
                "activity_level": 0.7,
                "posts_per_hour": 0.5,
                "comments_per_hour": 1.2,
                "active_hours": [9, 10, 11, 12, 13, 18, 19, 20, 21, 22, 23],  # 白天+晚間
                "response_delay_min": 2,
                "response_delay_max": 20,
                "sentiment_bias": 0.0,
                "stance": "neutral",
                "influence_weight": 1.0
            }
    

