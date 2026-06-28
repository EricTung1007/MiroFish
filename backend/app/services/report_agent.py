"""
Report Agent服務
使用LangChain + Zep實現ReACT模式的模擬報告生成

功能：
1. 根據模擬需求和Zep圖譜信息生成報告
2. 先規劃目錄結構，然後分段生成
3. 每段採用ReACT多輪思考與反思模式
4. 支持與用戶對話，在對話中自主調用檢索工具
"""

import os
import json
import time
import re
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..config import Config
from ..utils.llm_client import LLMClient
from ..utils.logger import get_logger
from ..utils.locale import get_language_instruction, t
from .zep_tools import (
    ZepToolsService, 
    SearchResult, 
    InsightForgeResult, 
    PanoramaResult,
    InterviewResult
)

logger = get_logger('mirofish.report_agent')


class ReportLogger:
    """
    Report Agent 詳細日誌記錄器
    
    在報告文件夾中生成 agent_log.jsonl 文件，記錄每一步詳細動作。
    每行是一個完整的 JSON 對象，包含時間戳、動作類型、詳細內容等。
    """
    
    def __init__(self, report_id: str):
        """
        初始化日誌記錄器
        
        Args:
            report_id: 報告ID，用於確定日誌文件路徑
        """
        self.report_id = report_id
        self.log_file_path = os.path.join(
            Config.UPLOAD_FOLDER, 'reports', report_id, 'agent_log.jsonl'
        )
        self.start_time = datetime.now()
        self._ensure_log_file()
    
    def _ensure_log_file(self):
        """確保日誌文件所在目錄存在"""
        log_dir = os.path.dirname(self.log_file_path)
        os.makedirs(log_dir, exist_ok=True)
    
    def _get_elapsed_time(self) -> float:
        """獲取從開始到現在的耗時（秒）"""
        return (datetime.now() - self.start_time).total_seconds()
    
    def log(
        self, 
        action: str, 
        stage: str,
        details: Dict[str, Any],
        section_title: str = None,
        section_index: int = None
    ):
        """
        記錄一條日誌
        
        Args:
            action: 動作類型，如 'start', 'tool_call', 'llm_response', 'section_complete' 等
            stage: 當前階段，如 'planning', 'generating', 'completed'
            details: 詳細內容字典，不截斷
            section_title: 當前章節標題（可選）
            section_index: 當前章節索引（可選）
        """
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "elapsed_seconds": round(self._get_elapsed_time(), 2),
            "report_id": self.report_id,
            "action": action,
            "stage": stage,
            "section_title": section_title,
            "section_index": section_index,
            "details": details
        }
        
        # 追加寫入 JSONL 文件
        with open(self.log_file_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
    
    def log_start(self, simulation_id: str, graph_id: str, simulation_requirement: str):
        """記錄報告生成開始"""
        self.log(
            action="report_start",
            stage="pending",
            details={
                "simulation_id": simulation_id,
                "graph_id": graph_id,
                "simulation_requirement": simulation_requirement,
                "message": t('report.taskStarted')
            }
        )
    
    def log_planning_start(self):
        """記錄大綱規劃開始"""
        self.log(
            action="planning_start",
            stage="planning",
            details={"message": t('report.planningStart')}
        )
    
    def log_planning_context(self, context: Dict[str, Any]):
        """記錄規劃時獲取的上下文信息"""
        self.log(
            action="planning_context",
            stage="planning",
            details={
                "message": t('report.fetchSimContext'),
                "context": context
            }
        )
    
    def log_planning_complete(self, outline_dict: Dict[str, Any]):
        """記錄大綱規劃完成"""
        self.log(
            action="planning_complete",
            stage="planning",
            details={
                "message": t('report.planningComplete'),
                "outline": outline_dict
            }
        )
    
    def log_section_start(self, section_title: str, section_index: int):
        """記錄章節生成開始"""
        self.log(
            action="section_start",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={"message": t('report.sectionStart', title=section_title)}
        )
    
    def log_react_thought(self, section_title: str, section_index: int, iteration: int, thought: str):
        """記錄 ReACT 思考過程"""
        self.log(
            action="react_thought",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "iteration": iteration,
                "thought": thought,
                "message": t('report.reactThought', iteration=iteration)
            }
        )
    
    def log_tool_call(
        self, 
        section_title: str, 
        section_index: int,
        tool_name: str, 
        parameters: Dict[str, Any],
        iteration: int
    ):
        """記錄工具調用"""
        self.log(
            action="tool_call",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "iteration": iteration,
                "tool_name": tool_name,
                "parameters": parameters,
                "message": t('report.toolCall', toolName=tool_name)
            }
        )
    
    def log_tool_result(
        self,
        section_title: str,
        section_index: int,
        tool_name: str,
        result: str,
        iteration: int
    ):
        """記錄工具調用結果（完整內容，不截斷）"""
        self.log(
            action="tool_result",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "iteration": iteration,
                "tool_name": tool_name,
                "result": result,  # 完整結果，不截斷
                "result_length": len(result),
                "message": t('report.toolResult', toolName=tool_name)
            }
        )
    
    def log_llm_response(
        self,
        section_title: str,
        section_index: int,
        response: str,
        iteration: int,
        has_tool_calls: bool,
        has_final_answer: bool
    ):
        """記錄 LLM 響應（完整內容，不截斷）"""
        self.log(
            action="llm_response",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "iteration": iteration,
                "response": response,  # 完整響應，不截斷
                "response_length": len(response),
                "has_tool_calls": has_tool_calls,
                "has_final_answer": has_final_answer,
                "message": t('report.llmResponse', hasToolCalls=has_tool_calls, hasFinalAnswer=has_final_answer)
            }
        )
    
    def log_section_content(
        self,
        section_title: str,
        section_index: int,
        content: str,
        tool_calls_count: int
    ):
        """記錄章節內容生成完成（僅記錄內容，不代表整個章節完成）"""
        self.log(
            action="section_content",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "content": content,  # 完整內容，不截斷
                "content_length": len(content),
                "tool_calls_count": tool_calls_count,
                "message": t('report.sectionContentDone', title=section_title)
            }
        )
    
    def log_section_full_complete(
        self,
        section_title: str,
        section_index: int,
        full_content: str
    ):
        """
        記錄章節生成完成

        前端應監聽此日誌來判斷一個章節是否真正完成，並獲取完整內容
        """
        self.log(
            action="section_complete",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "content": full_content,
                "content_length": len(full_content),
                "message": t('report.sectionComplete', title=section_title)
            }
        )
    
    def log_report_complete(self, total_sections: int, total_time_seconds: float):
        """記錄報告生成完成"""
        self.log(
            action="report_complete",
            stage="completed",
            details={
                "total_sections": total_sections,
                "total_time_seconds": round(total_time_seconds, 2),
                "message": t('report.reportComplete')
            }
        )
    
    def log_error(self, error_message: str, stage: str, section_title: str = None):
        """記錄錯誤"""
        self.log(
            action="error",
            stage=stage,
            section_title=section_title,
            section_index=None,
            details={
                "error": error_message,
                "message": t('report.errorOccurred', error=error_message)
            }
        )


class ReportConsoleLogger:
    """
    Report Agent 控制檯日誌記錄器
    
    將控制檯風格的日誌（INFO、WARNING等）寫入報告文件夾中的 console_log.txt 文件。
    這些日誌與 agent_log.jsonl 不同，是純文本格式的控制檯輸出。
    """
    
    def __init__(self, report_id: str):
        """
        初始化控制檯日誌記錄器
        
        Args:
            report_id: 報告ID，用於確定日誌文件路徑
        """
        self.report_id = report_id
        self.log_file_path = os.path.join(
            Config.UPLOAD_FOLDER, 'reports', report_id, 'console_log.txt'
        )
        self._ensure_log_file()
        self._file_handler = None
        self._setup_file_handler()
    
    def _ensure_log_file(self):
        """確保日誌文件所在目錄存在"""
        log_dir = os.path.dirname(self.log_file_path)
        os.makedirs(log_dir, exist_ok=True)
    
    def _setup_file_handler(self):
        """設置文件處理器，將日誌同時寫入文件"""
        import logging
        
        # 創建文件處理器
        self._file_handler = logging.FileHandler(
            self.log_file_path,
            mode='a',
            encoding='utf-8'
        )
        self._file_handler.setLevel(logging.INFO)
        
        # 使用與控制檯相同的簡潔格式
        formatter = logging.Formatter(
            '[%(asctime)s] %(levelname)s: %(message)s',
            datefmt='%H:%M:%S'
        )
        self._file_handler.setFormatter(formatter)
        
        # 添加到 report_agent 相關的 logger
        loggers_to_attach = [
            'mirofish.report_agent',
            'mirofish.zep_tools',
        ]
        
        for logger_name in loggers_to_attach:
            target_logger = logging.getLogger(logger_name)
            # 避免重複添加
            if self._file_handler not in target_logger.handlers:
                target_logger.addHandler(self._file_handler)
    
    def close(self):
        """關閉文件處理器並從 logger 中移除"""
        import logging
        
        if self._file_handler:
            loggers_to_detach = [
                'mirofish.report_agent',
                'mirofish.zep_tools',
            ]
            
            for logger_name in loggers_to_detach:
                target_logger = logging.getLogger(logger_name)
                if self._file_handler in target_logger.handlers:
                    target_logger.removeHandler(self._file_handler)
            
            self._file_handler.close()
            self._file_handler = None
    
    def __del__(self):
        """析構時確保關閉文件處理器"""
        self.close()


class ReportStatus(str, Enum):
    """報告狀態"""
    PENDING = "pending"
    PLANNING = "planning"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ReportSection:
    """報告章節"""
    title: str
    content: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "content": self.content
        }

    def to_markdown(self, level: int = 2) -> str:
        """轉換為Markdown格式"""
        md = f"{'#' * level} {self.title}\n\n"
        if self.content:
            md += f"{self.content}\n\n"
        return md


@dataclass
class ReportOutline:
    """報告大綱"""
    title: str
    summary: str
    sections: List[ReportSection]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "sections": [s.to_dict() for s in self.sections]
        }
    
    def to_markdown(self) -> str:
        """轉換為Markdown格式"""
        md = f"# {self.title}\n\n"
        md += f"> {self.summary}\n\n"
        for section in self.sections:
            md += section.to_markdown()
        return md


@dataclass
class Report:
    """完整報告"""
    report_id: str
    simulation_id: str
    graph_id: str
    simulation_requirement: str
    status: ReportStatus
    outline: Optional[ReportOutline] = None
    markdown_content: str = ""
    created_at: str = ""
    completed_at: str = ""
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "report_id": self.report_id,
            "simulation_id": self.simulation_id,
            "graph_id": self.graph_id,
            "simulation_requirement": self.simulation_requirement,
            "status": self.status.value,
            "outline": self.outline.to_dict() if self.outline else None,
            "markdown_content": self.markdown_content,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "error": self.error
        }


# ═══════════════════════════════════════════════════════════════
# Prompt 模板常量
# ═══════════════════════════════════════════════════════════════

# ── 工具描述 ──

TOOL_DESC_INSIGHT_FORGE = """\
【深度洞察檢索 - 強大的檢索工具】
這是我們強大的檢索函數，專為深度分析設計。它會：
1. 自動將你的問題分解為多個子問題
2. 從多個維度檢索模擬圖譜中的信息
3. 整合語義搜索、實體分析、關係鏈追蹤的結果
4. 返回最全面、最深度的檢索內容

【使用場景】
- 需要深入分析某個話題
- 需要了解事件的多個方面
- 需要獲取支撐報告章節的豐富素材

【返回內容】
- 相關事實原文（可直接引用）
- 核心實體洞察
- 關係鏈分析"""

TOOL_DESC_PANORAMA_SEARCH = """\
【廣度搜索 - 獲取全貌視圖】
這個工具用於獲取模擬結果的完整全貌，特別適合瞭解事件演變過程。它會：
1. 獲取所有相關節點和關係
2. 區分當前有效的事實和歷史/過期的事實
3. 幫助你瞭解輿情是如何演變的

【使用場景】
- 需要了解事件的完整發展脈絡
- 需要對比不同階段的輿情變化
- 需要獲取全面的實體和關係信息

【返回內容】
- 當前有效事實（模擬最新結果）
- 歷史/過期事實（演變記錄）
- 所有涉及的實體"""

TOOL_DESC_QUICK_SEARCH = """\
【簡單搜索 - 快速檢索】
輕量級的快速檢索工具，適合簡單、直接的信息查詢。

【使用場景】
- 需要快速查找某個具體信息
- 需要驗證某個事實
- 簡單的信息檢索

【返回內容】
- 與查詢最相關的事實列表"""

TOOL_DESC_INTERVIEW_AGENTS = """\
【深度採訪 - 真實Agent採訪（雙平臺）】
調用OASIS模擬環境的採訪API，對正在運行的模擬Agent進行真實採訪！
這不是LLM模擬，而是調用真實的採訪接口獲取模擬Agent的原始回答。
默認在Twitter和Reddit兩個平臺同時採訪，獲取更全面的觀點。

功能流程：
1. 自動讀取人設文件，瞭解所有模擬Agent
2. 智能選擇與採訪主題最相關的Agent（如學生、媒體、官方等）
3. 自動生成採訪問題
4. 調用 /api/simulation/interview/batch 接口在雙平臺進行真實採訪
5. 整合所有采訪結果，提供多視角分析

【使用場景】
- 需要從不同角色視角瞭解事件看法（學生怎麼看？媒體怎麼看？官方怎麼說？）
- 需要收集多方意見和立場
- 需要獲取模擬Agent的真實回答（來自OASIS模擬環境）
- 想讓報告更生動，包含"採訪實錄"

【返回內容】
- 被採訪Agent的身份信息
- 各Agent在Twitter和Reddit兩個平臺的採訪回答
- 關鍵引言（可直接引用）
- 採訪摘要和觀點對比

【重要】需要OASIS模擬環境正在運行才能使用此功能！"""

# ── 大綱規劃 prompt ──

PLAN_SYSTEM_PROMPT = """\
你是一個「未來預測報告」的撰寫專家，擁有對模擬世界的「上帝視角」——你可以洞察模擬中每一位Agent的行為、言論和互動。

【核心理念】
我們構建了一個模擬世界，並向其中注入了特定的「模擬需求」作為變量。模擬世界的演化結果，就是對未來可能發生情況的預測。你正在觀察的不是"實驗數據"，而是"未來的預演"。

【你的任務】
撰寫一份「未來預測報告」，回答：
1. 在我們設定的條件下，未來發生了什麼？
2. 各類Agent（人群）是如何反應和行動？
3. 這個模擬揭示了哪些值得關注的未來趨勢和風險？

【報告定位】
- ✅ 這是一份基於模擬的未來預測報告，揭示"如果這樣，未來會怎樣"
- ✅ 聚焦於預測結果：事件走向、群體反應、湧現現象、潛在風險
- ✅ 模擬世界中的Agent言行就是對未來人群行為的預測
- ❌ 不是對現實世界現狀的分析
- ❌ 不是泛泛而談的輿情綜述

【章節數量限制】
- 最少2個章節，最多5個章節
- 不需要子章節，每個章節直接撰寫完整內容
- 內容要精煉，聚焦於核心預測發現
- 章節結構由你根據預測結果自主設計

請輸出JSON格式的報告大綱，格式如下：
{
    "title": "報告標題",
    "summary": "報告摘要（一句話概括核心預測發現）",
    "sections": [
        {
            "title": "章節標題",
            "description": "章節內容描述"
        }
    ]
}

注意：sections數組最少2個，最多5個元素！"""

PLAN_USER_PROMPT_TEMPLATE = """\
【預測場景設定】
我們向模擬世界注入的變量（模擬需求）：{simulation_requirement}

【模擬世界規模】
- 參與模擬的實體數量: {total_nodes}
- 實體間產生的關係數量: {total_edges}
- 實體類型分佈: {entity_types}
- 活躍Agent數量: {total_entities}

【模擬預測到的部分未來事實樣本】
{related_facts_json}

請以「上帝視角」審視這個未來預演：
1. 在我們設定的條件下，未來呈現出了什麼樣的狀態？
2. 各類人群（Agent）是如何反應和行動的？
3. 這個模擬揭示了哪些值得關注的未來趨勢？

根據預測結果，設計最合適的報告章節結構。

【再次提醒】報告章節數量：最少2個，最多5個，內容要精煉聚焦於核心預測發現。"""

# ── 章節生成 prompt ──

SECTION_SYSTEM_PROMPT_TEMPLATE = """\
你是一個「未來預測報告」的撰寫專家，正在撰寫報告的一個章節。

報告標題: {report_title}
報告摘要: {report_summary}
預測場景（模擬需求）: {simulation_requirement}

當前要撰寫的章節: {section_title}

═══════════════════════════════════════════════════════════════
【核心理念】
═══════════════════════════════════════════════════════════════

模擬世界是對未來的預演。我們向模擬世界注入了特定條件（模擬需求），
模擬中Agent的行為和互動，就是對未來人群行為的預測。

你的任務是：
- 揭示在設定條件下，未來發生了什麼
- 預測各類人群（Agent）是如何反應和行動的
- 發現值得關注的未來趨勢、風險和機會

❌ 不要寫成對現實世界現狀的分析
✅ 要聚焦於"未來會怎樣"——模擬結果就是預測的未來

═══════════════════════════════════════════════════════════════
【最重要的規則 - 必須遵守】
═══════════════════════════════════════════════════════════════

1. 【必須調用工具觀察模擬世界】
   - 你正在以「上帝視角」觀察未來的預演
   - 所有內容必須來自模擬世界中發生的事件和Agent言行
   - 禁止使用你自己的知識來編寫報告內容
   - 每個章節至少調用3次工具（最多5次）來觀察模擬的世界，它代表了未來

2. 【必須引用Agent的原始言行】
   - Agent的發言和行為是對未來人群行為的預測
   - 在報告中使用引用格式展示這些預測，例如：
     > "某類人群會表示：原文內容..."
   - 這些引用是模擬預測的核心證據

3. 【語言一致性 - 引用內容必須翻譯為報告語言】
   - 工具返回的內容可能包含與報告語言不同的表述
   - 報告必須全部使用與用戶指定語言一致的語言撰寫
   - 當你引用工具返回的其他語言內容時，必須將其翻譯為報告語言後再寫入
   - 翻譯時保持原意不變，確保表述自然通順
   - 這一規則同時適用於正文和引用塊（> 格式）中的內容

4. 【忠實呈現預測結果】
   - 報告內容必須反映模擬世界中的代表未來的模擬結果
   - 不要添加模擬中不存在的信息
   - 如果某方面信息不足，如實說明

═══════════════════════════════════════════════════════════════
【⚠️ 格式規範 - 極其重要！】
═══════════════════════════════════════════════════════════════

【一個章節 = 最小內容單位】
- 每個章節是報告的最小分塊單位
- ❌ 禁止在章節內使用任何 Markdown 標題（#、##、###、#### 等）
- ❌ 禁止在內容開頭添加章節主標題
- ✅ 章節標題由系統自動添加，你只需撰寫純正文內容
- ✅ 使用**粗體**、段落分隔、引用、列表來組織內容，但不要用標題

【正確示例】
```
本章節分析了事件的輿論傳播態勢。通過對模擬數據的深入分析，我們發現...

**首發引爆階段**

微博作為輿情的第一現場，承擔了信息首發的核心功能：

> "微博貢獻了68%的首發聲量..."

**情緒放大階段**

抖音平臺進一步放大了事件影響力：

- 視覺衝擊力強
- 情緒共鳴度高
```

【錯誤示例】
```
## 執行摘要          ← 錯誤！不要添加任何標題
### 一、首發階段     ← 錯誤！不要用###分小節
#### 1.1 詳細分析   ← 錯誤！不要用####細分

本章節分析了...
```

═══════════════════════════════════════════════════════════════
【可用檢索工具】（每章節調用3-5次）
═══════════════════════════════════════════════════════════════

{tools_description}

【工具使用建議 - 請混合使用不同工具，不要只用一種】
- insight_forge: 深度洞察分析，自動分解問題並多維度檢索事實和關係
- panorama_search: 廣角全景搜索，瞭解事件全貌、時間線和演變過程
- quick_search: 快速驗證某個具體信息點
- interview_agents: 採訪模擬Agent，獲取不同角色的第一人稱觀點和真實反應

═══════════════════════════════════════════════════════════════
【工作流程】
═══════════════════════════════════════════════════════════════

每次回覆你只能做以下兩件事之一（不可同時做）：

選項A - 調用工具：
輸出你的思考，然後用以下格式調用一個工具：
<tool_call>
{{"name": "工具名稱", "parameters": {{"參數名": "參數值"}}}}
</tool_call>
系統會執行工具並把結果返回給你。你不需要也不能自己編寫工具返回結果。

選項B - 輸出最終內容：
當你已通過工具獲取了足夠信息，以 "Final Answer:" 開頭輸出章節內容。

⚠️ 嚴格禁止：
- 禁止在一次回覆中同時包含工具調用和 Final Answer
- 禁止自己編造工具返回結果（Observation），所有工具結果由系統注入
- 每次回覆最多調用一個工具

═══════════════════════════════════════════════════════════════
【章節內容要求】
═══════════════════════════════════════════════════════════════

1. 內容必須基於工具檢索到的模擬數據
2. 大量引用原文來展示模擬效果
3. 使用Markdown格式（但禁止使用標題）：
   - 使用 **粗體文字** 標記重點（代替子標題）
   - 使用列表（-或1.2.3.）組織要點
   - 使用空行分隔不同段落
   - ❌ 禁止使用 #、##、###、#### 等任何標題語法
4. 【引用格式規範 - 必須單獨成段】
   引用必須獨立成段，前後各有一個空行，不能混在段落中：

   ✅ 正確格式：
   ```
   校方的回應被認為缺乏實質內容。

   > "校方的應對模式在瞬息萬變的社交媒體環境中顯得僵化和遲緩。"

   這一評價反映了公眾的普遍不滿。
   ```

   ❌ 錯誤格式：
   ```
   校方的回應被認為缺乏實質內容。> "校方的應對模式..." 這一評價反映了...
   ```
5. 保持與其他章節的邏輯連貫性
6. 【避免重複】仔細閱讀下方已完成的章節內容，不要重複描述相同的信息
7. 【再次強調】不要添加任何標題！用**粗體**代替小節標題"""

SECTION_USER_PROMPT_TEMPLATE = """\
已完成的章節內容（請仔細閱讀，避免重複）：
{previous_content}

═══════════════════════════════════════════════════════════════
【當前任務】撰寫章節: {section_title}
═══════════════════════════════════════════════════════════════

【重要提醒】
1. 仔細閱讀上方已完成的章節，避免重複相同的內容！
2. 開始前必須先調用工具獲取模擬數據
3. 請混合使用不同工具，不要只用一種
4. 報告內容必須來自檢索結果，不要使用自己的知識

【⚠️ 格式警告 - 必須遵守】
- ❌ 不要寫任何標題（#、##、###、####都不行）
- ❌ 不要寫"{section_title}"作為開頭
- ✅ 章節標題由系統自動添加
- ✅ 直接寫正文，用**粗體**代替小節標題

請開始：
1. 首先思考（Thought）這個章節需要什麼信息
2. 然後調用工具（Action）獲取模擬數據
3. 收集足夠信息後輸出 Final Answer（純正文，無任何標題）"""

# ── ReACT 循環內消息模板 ──

REACT_OBSERVATION_TEMPLATE = """\
Observation（檢索結果）:

═══ 工具 {tool_name} 返回 ═══
{result}

═══════════════════════════════════════════════════════════════
已調用工具 {tool_calls_count}/{max_tool_calls} 次（已用: {used_tools_str}）{unused_hint}
- 如果信息充分：以 "Final Answer:" 開頭輸出章節內容（必須引用上述原文）
- 如果需要更多信息：調用一個工具繼續檢索
═══════════════════════════════════════════════════════════════"""

REACT_INSUFFICIENT_TOOLS_MSG = (
    "【注意】你只調用了{tool_calls_count}次工具，至少需要{min_tool_calls}次。"
    "請再調用工具獲取更多模擬數據，然後再輸出 Final Answer。{unused_hint}"
)

REACT_INSUFFICIENT_TOOLS_MSG_ALT = (
    "當前只調用了 {tool_calls_count} 次工具，至少需要 {min_tool_calls} 次。"
    "請調用工具獲取模擬數據。{unused_hint}"
)

REACT_TOOL_LIMIT_MSG = (
    "工具調用次數已達上限（{tool_calls_count}/{max_tool_calls}），不能再調用工具。"
    '請立即基於已獲取的信息，以 "Final Answer:" 開頭輸出章節內容。'
)

REACT_UNUSED_TOOLS_HINT = "\n💡 你還沒有使用過: {unused_list}，建議嘗試不同工具獲取多角度信息"

REACT_FORCE_FINAL_MSG = "已達到工具調用限制，請直接輸出 Final Answer: 並生成章節內容。"

# ── Chat prompt ──

CHAT_SYSTEM_PROMPT_TEMPLATE = """\
你是一個簡潔高效的模擬預測助手。

【背景】
預測條件: {simulation_requirement}

【已生成的分析報告】
{report_content}

【規則】
1. 優先基於上述報告內容回答問題
2. 直接回答問題，避免冗長的思考論述
3. 僅在報告內容不足以回答時，才調用工具檢索更多數據
4. 回答要簡潔、清晰、有條理

【可用工具】（僅在需要時使用，最多調用1-2次）
{tools_description}

【工具調用格式】
<tool_call>
{{"name": "工具名稱", "parameters": {{"參數名": "參數值"}}}}
</tool_call>

【回答風格】
- 簡潔直接，不要長篇大論
- 使用 > 格式引用關鍵內容
- 優先給出結論，再解釋原因"""

CHAT_OBSERVATION_SUFFIX = "\n\n請簡潔回答問題。"


# ═══════════════════════════════════════════════════════════════
# ReportAgent 主類
# ═══════════════════════════════════════════════════════════════


class ReportAgent:
    """
    Report Agent - 模擬報告生成Agent

    採用ReACT（Reasoning + Acting）模式：
    1. 規劃階段：分析模擬需求，規劃報告目錄結構
    2. 生成階段：逐章節生成內容，每章節可多次調用工具獲取信息
    3. 反思階段：檢查內容完整性和準確性
    """
    
    # 最大工具調用次數（每個章節）
    MAX_TOOL_CALLS_PER_SECTION = 5
    
    # 最大反思輪數
    MAX_REFLECTION_ROUNDS = 3
    
    # 對話中的最大工具調用次數
    MAX_TOOL_CALLS_PER_CHAT = 2
    
    def __init__(
        self, 
        graph_id: str,
        simulation_id: str,
        simulation_requirement: str,
        llm_client: Optional[LLMClient] = None,
        zep_tools: Optional[ZepToolsService] = None
    ):
        """
        初始化Report Agent
        
        Args:
            graph_id: 圖譜ID
            simulation_id: 模擬ID
            simulation_requirement: 模擬需求描述
            llm_client: LLM客戶端（可選）
            zep_tools: Zep工具服務（可選）
        """
        self.graph_id = graph_id
        self.simulation_id = simulation_id
        self.simulation_requirement = simulation_requirement
        
        self.llm = llm_client or LLMClient()
        self.zep_tools = zep_tools or ZepToolsService()
        
        # 工具定義
        self.tools = self._define_tools()
        
        # 日誌記錄器（在 generate_report 中初始化）
        self.report_logger: Optional[ReportLogger] = None
        # 控制檯日誌記錄器（在 generate_report 中初始化）
        self.console_logger: Optional[ReportConsoleLogger] = None
        
        logger.info(t('report.agentInitDone', graphId=graph_id, simulationId=simulation_id))
    
    def _define_tools(self) -> Dict[str, Dict[str, Any]]:
        """定義可用工具"""
        return {
            "insight_forge": {
                "name": "insight_forge",
                "description": TOOL_DESC_INSIGHT_FORGE,
                "parameters": {
                    "query": "你想深入分析的問題或話題",
                    "report_context": "當前報告章節的上下文（可選，有助於生成更精準的子問題）"
                }
            },
            "panorama_search": {
                "name": "panorama_search",
                "description": TOOL_DESC_PANORAMA_SEARCH,
                "parameters": {
                    "query": "搜索查詢，用於相關性排序",
                    "include_expired": "是否包含過期/歷史內容（默認True）"
                }
            },
            "quick_search": {
                "name": "quick_search",
                "description": TOOL_DESC_QUICK_SEARCH,
                "parameters": {
                    "query": "搜索查詢字符串",
                    "limit": "返回結果數量（可選，默認10）"
                }
            },
            "interview_agents": {
                "name": "interview_agents",
                "description": TOOL_DESC_INTERVIEW_AGENTS,
                "parameters": {
                    "interview_topic": "採訪主題或需求描述（如：'瞭解學生對宿舍甲醛事件的看法'）",
                    "max_agents": "最多采訪的Agent數量（可選，默認5，最大10）"
                }
            }
        }
    
    def _execute_tool(self, tool_name: str, parameters: Dict[str, Any], report_context: str = "") -> str:
        """
        執行工具調用
        
        Args:
            tool_name: 工具名稱
            parameters: 工具參數
            report_context: 報告上下文（用於InsightForge）
            
        Returns:
            工具執行結果（文本格式）
        """
        logger.info(t('report.executingTool', toolName=tool_name, params=parameters))
        
        try:
            if tool_name == "insight_forge":
                query = parameters.get("query", "")
                ctx = parameters.get("report_context", "") or report_context
                result = self.zep_tools.insight_forge(
                    graph_id=self.graph_id,
                    query=query,
                    simulation_requirement=self.simulation_requirement,
                    report_context=ctx
                )
                return result.to_text()
            
            elif tool_name == "panorama_search":
                # 廣度搜索 - 獲取全貌
                query = parameters.get("query", "")
                include_expired = parameters.get("include_expired", True)
                if isinstance(include_expired, str):
                    include_expired = include_expired.lower() in ['true', '1', 'yes']
                result = self.zep_tools.panorama_search(
                    graph_id=self.graph_id,
                    query=query,
                    include_expired=include_expired
                )
                return result.to_text()
            
            elif tool_name == "quick_search":
                # 簡單搜索 - 快速檢索
                query = parameters.get("query", "")
                limit = parameters.get("limit", 10)
                if isinstance(limit, str):
                    limit = int(limit)
                result = self.zep_tools.quick_search(
                    graph_id=self.graph_id,
                    query=query,
                    limit=limit
                )
                return result.to_text()
            
            elif tool_name == "interview_agents":
                # 深度採訪 - 調用真實的OASIS採訪API獲取模擬Agent的回答（雙平臺）
                interview_topic = parameters.get("interview_topic", parameters.get("query", ""))
                max_agents = parameters.get("max_agents", 5)
                if isinstance(max_agents, str):
                    max_agents = int(max_agents)
                max_agents = min(max_agents, 10)
                result = self.zep_tools.interview_agents(
                    simulation_id=self.simulation_id,
                    interview_requirement=interview_topic,
                    simulation_requirement=self.simulation_requirement,
                    max_agents=max_agents
                )
                return result.to_text()
            
            # ========== 向後兼容的舊工具（內部重定向到新工具） ==========
            
            elif tool_name == "search_graph":
                # 重定向到 quick_search
                logger.info(t('report.redirectToQuickSearch'))
                return self._execute_tool("quick_search", parameters, report_context)
            
            elif tool_name == "get_graph_statistics":
                result = self.zep_tools.get_graph_statistics(self.graph_id)
                return json.dumps(result, ensure_ascii=False, indent=2)
            
            elif tool_name == "get_entity_summary":
                entity_name = parameters.get("entity_name", "")
                result = self.zep_tools.get_entity_summary(
                    graph_id=self.graph_id,
                    entity_name=entity_name
                )
                return json.dumps(result, ensure_ascii=False, indent=2)
            
            elif tool_name == "get_simulation_context":
                # 重定向到 insight_forge，因為它更強大
                logger.info(t('report.redirectToInsightForge'))
                query = parameters.get("query", self.simulation_requirement)
                return self._execute_tool("insight_forge", {"query": query}, report_context)
            
            elif tool_name == "get_entities_by_type":
                entity_type = parameters.get("entity_type", "")
                nodes = self.zep_tools.get_entities_by_type(
                    graph_id=self.graph_id,
                    entity_type=entity_type
                )
                result = [n.to_dict() for n in nodes]
                return json.dumps(result, ensure_ascii=False, indent=2)
            
            else:
                return f"未知工具: {tool_name}。請使用以下工具之一: insight_forge, panorama_search, quick_search"
                
        except Exception as e:
            logger.error(t('report.toolExecFailed', toolName=tool_name, error=str(e)))
            return f"工具執行失敗: {str(e)}"
    
    # 合法的工具名稱集合，用於裸 JSON 兜底解析時校驗
    VALID_TOOL_NAMES = {"insight_forge", "panorama_search", "quick_search", "interview_agents"}

    def _parse_tool_calls(self, response: str) -> List[Dict[str, Any]]:
        """
        從LLM響應中解析工具調用

        支持的格式（按優先級）：
        1. <tool_call>{"name": "tool_name", "parameters": {...}}</tool_call>
        2. 裸 JSON（響應整體或單行就是一個工具調用 JSON）
        """
        tool_calls = []

        # 格式1: XML風格（標準格式）
        xml_pattern = r'<tool_call>\s*(\{.*?\})\s*</tool_call>'
        for match in re.finditer(xml_pattern, response, re.DOTALL):
            try:
                call_data = json.loads(match.group(1))
                tool_calls.append(call_data)
            except json.JSONDecodeError:
                pass

        if tool_calls:
            return tool_calls

        # 格式2: 兜底 - LLM 直接輸出裸 JSON（沒包 <tool_call> 標籤）
        # 只在格式1未匹配時嘗試，避免誤匹配正文中的 JSON
        stripped = response.strip()
        if stripped.startswith('{') and stripped.endswith('}'):
            try:
                call_data = json.loads(stripped)
                if self._is_valid_tool_call(call_data):
                    tool_calls.append(call_data)
                    return tool_calls
            except json.JSONDecodeError:
                pass

        # 響應可能包含思考文字 + 裸 JSON，嘗試提取最後一個 JSON 對象
        json_pattern = r'(\{"(?:name|tool)"\s*:.*?\})\s*$'
        match = re.search(json_pattern, stripped, re.DOTALL)
        if match:
            try:
                call_data = json.loads(match.group(1))
                if self._is_valid_tool_call(call_data):
                    tool_calls.append(call_data)
            except json.JSONDecodeError:
                pass

        return tool_calls

    def _is_valid_tool_call(self, data: dict) -> bool:
        """校驗解析出的 JSON 是否是合法的工具調用"""
        # 支持 {"name": ..., "parameters": ...} 和 {"tool": ..., "params": ...} 兩種鍵名
        tool_name = data.get("name") or data.get("tool")
        if tool_name and tool_name in self.VALID_TOOL_NAMES:
            # 統一鍵名為 name / parameters
            if "tool" in data:
                data["name"] = data.pop("tool")
            if "params" in data and "parameters" not in data:
                data["parameters"] = data.pop("params")
            return True
        return False
    
    def _get_tools_description(self) -> str:
        """生成工具描述文本"""
        desc_parts = ["可用工具："]
        for name, tool in self.tools.items():
            params_desc = ", ".join([f"{k}: {v}" for k, v in tool["parameters"].items()])
            desc_parts.append(f"- {name}: {tool['description']}")
            if params_desc:
                desc_parts.append(f"  參數: {params_desc}")
        return "\n".join(desc_parts)
    
    def plan_outline(
        self, 
        progress_callback: Optional[Callable] = None
    ) -> ReportOutline:
        """
        規劃報告大綱
        
        使用LLM分析模擬需求，規劃報告的目錄結構
        
        Args:
            progress_callback: 進度回調函數
            
        Returns:
            ReportOutline: 報告大綱
        """
        logger.info(t('report.startPlanningOutline'))
        
        if progress_callback:
            progress_callback("planning", 0, t('progress.analyzingRequirements'))
        
        # 首先獲取模擬上下文
        context = self.zep_tools.get_simulation_context(
            graph_id=self.graph_id,
            simulation_requirement=self.simulation_requirement
        )
        
        if progress_callback:
            progress_callback("planning", 30, t('progress.generatingOutline'))
        
        system_prompt = f"{PLAN_SYSTEM_PROMPT}\n\n{get_language_instruction()}"
        user_prompt = PLAN_USER_PROMPT_TEMPLATE.format(
            simulation_requirement=self.simulation_requirement,
            total_nodes=context.get('graph_statistics', {}).get('total_nodes', 0),
            total_edges=context.get('graph_statistics', {}).get('total_edges', 0),
            entity_types=list(context.get('graph_statistics', {}).get('entity_types', {}).keys()),
            total_entities=context.get('total_entities', 0),
            related_facts_json=json.dumps(context.get('related_facts', [])[:10], ensure_ascii=False, indent=2),
        )

        try:
            response = self.llm.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3
            )
            
            if progress_callback:
                progress_callback("planning", 80, t('progress.parsingOutline'))
            
            # 解析大綱
            sections = []
            for section_data in response.get("sections", []):
                sections.append(ReportSection(
                    title=section_data.get("title", ""),
                    content=""
                ))
            
            outline = ReportOutline(
                title=response.get("title", "模擬分析報告"),
                summary=response.get("summary", ""),
                sections=sections
            )
            
            if progress_callback:
                progress_callback("planning", 100, t('progress.outlinePlanComplete'))
            
            logger.info(t('report.outlinePlanDone', count=len(sections)))
            return outline
            
        except Exception as e:
            logger.error(t('report.outlinePlanFailed', error=str(e)))
            # 返回默認大綱（3個章節，作為fallback）
            return ReportOutline(
                title="未來預測報告",
                summary="基於模擬預測的未來趨勢與風險分析",
                sections=[
                    ReportSection(title="預測場景與核心發現"),
                    ReportSection(title="人群行為預測分析"),
                    ReportSection(title="趨勢展望與風險提示")
                ]
            )
    
    def _generate_section_react(
        self, 
        section: ReportSection,
        outline: ReportOutline,
        previous_sections: List[str],
        progress_callback: Optional[Callable] = None,
        section_index: int = 0
    ) -> str:
        """
        使用ReACT模式生成單個章節內容
        
        ReACT循環：
        1. Thought（思考）- 分析需要什麼信息
        2. Action（行動）- 調用工具獲取信息
        3. Observation（觀察）- 分析工具返回結果
        4. 重複直到信息足夠或達到最大次數
        5. Final Answer（最終回答）- 生成章節內容
        
        Args:
            section: 要生成的章節
            outline: 完整大綱
            previous_sections: 之前章節的內容（用於保持連貫性）
            progress_callback: 進度回調
            section_index: 章節索引（用於日誌記錄）
            
        Returns:
            章節內容（Markdown格式）
        """
        logger.info(t('report.reactGenerateSection', title=section.title))
        
        # 記錄章節開始日誌
        if self.report_logger:
            self.report_logger.log_section_start(section.title, section_index)
        
        system_prompt = SECTION_SYSTEM_PROMPT_TEMPLATE.format(
            report_title=outline.title,
            report_summary=outline.summary,
            simulation_requirement=self.simulation_requirement,
            section_title=section.title,
            tools_description=self._get_tools_description(),
        )
        system_prompt = f"{system_prompt}\n\n{get_language_instruction()}"

        # 構建用戶prompt - 每個已完成章節各傳入最大4000字
        if previous_sections:
            previous_parts = []
            for sec in previous_sections:
                # 每個章節最多4000字
                truncated = sec[:4000] + "..." if len(sec) > 4000 else sec
                previous_parts.append(truncated)
            previous_content = "\n\n---\n\n".join(previous_parts)
        else:
            previous_content = "（這是第一個章節）"
        
        user_prompt = SECTION_USER_PROMPT_TEMPLATE.format(
            previous_content=previous_content,
            section_title=section.title,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        # ReACT循環
        tool_calls_count = 0
        max_iterations = 5  # 最大迭代輪數
        min_tool_calls = 3  # 最少工具調用次數
        conflict_retries = 0  # 工具調用與Final Answer同時出現的連續衝突次數
        used_tools = set()  # 記錄已調用過的工具名
        all_tools = {"insight_forge", "panorama_search", "quick_search", "interview_agents"}

        # 報告上下文，用於InsightForge的子問題生成
        report_context = f"章節標題: {section.title}\n模擬需求: {self.simulation_requirement}"
        
        for iteration in range(max_iterations):
            if progress_callback:
                progress_callback(
                    "generating", 
                    int((iteration / max_iterations) * 100),
                    t('progress.deepSearchAndWrite', current=tool_calls_count, max=self.MAX_TOOL_CALLS_PER_SECTION)
                )
            
            # 調用LLM
            response = self.llm.chat(
                messages=messages,
                temperature=0.5,
                max_tokens=4096
            )

            # 檢查 LLM 返回是否為 None（API 異常或內容為空）
            if response is None:
                logger.warning(t('report.sectionIterNone', title=section.title, iteration=iteration + 1))
                # 如果還有迭代次數，添加消息並重試
                if iteration < max_iterations - 1:
                    messages.append({"role": "assistant", "content": "（響應為空）"})
                    messages.append({"role": "user", "content": "請繼續生成內容。"})
                    continue
                # 最後一次迭代也返回 None，跳出循環進入強制收尾
                break

            logger.debug(f"LLM響應: {response[:200]}...")

            # 解析一次，複用結果
            tool_calls = self._parse_tool_calls(response)
            has_tool_calls = bool(tool_calls)
            has_final_answer = "Final Answer:" in response

            # ── 衝突處理：LLM 同時輸出了工具調用和 Final Answer ──
            if has_tool_calls and has_final_answer:
                conflict_retries += 1
                logger.warning(
                    t('report.sectionConflict', title=section.title, iteration=iteration+1, conflictCount=conflict_retries)
                )

                if conflict_retries <= 2:
                    # 前兩次：丟棄本次響應，要求 LLM 重新回覆
                    messages.append({"role": "assistant", "content": response})
                    messages.append({
                        "role": "user",
                        "content": (
                            "【格式錯誤】你在一次回覆中同時包含了工具調用和 Final Answer，這是不允許的。\n"
                            "每次回覆只能做以下兩件事之一：\n"
                            "- 調用一個工具（輸出一個 <tool_call> 塊，不要寫 Final Answer）\n"
                            "- 輸出最終內容（以 'Final Answer:' 開頭，不要包含 <tool_call>）\n"
                            "請重新回覆，只做其中一件事。"
                        ),
                    })
                    continue
                else:
                    # 第三次：降級處理，截斷到第一個工具調用，強制執行
                    logger.warning(
                        t('report.sectionConflictDowngrade', title=section.title, conflictCount=conflict_retries)
                    )
                    first_tool_end = response.find('</tool_call>')
                    if first_tool_end != -1:
                        response = response[:first_tool_end + len('</tool_call>')]
                        tool_calls = self._parse_tool_calls(response)
                        has_tool_calls = bool(tool_calls)
                    has_final_answer = False
                    conflict_retries = 0

            # 記錄 LLM 響應日誌
            if self.report_logger:
                self.report_logger.log_llm_response(
                    section_title=section.title,
                    section_index=section_index,
                    response=response,
                    iteration=iteration + 1,
                    has_tool_calls=has_tool_calls,
                    has_final_answer=has_final_answer
                )

            # ── 情況1：LLM 輸出了 Final Answer ──
            if has_final_answer:
                # 工具調用次數不足，拒絕並要求繼續調工具
                if tool_calls_count < min_tool_calls:
                    messages.append({"role": "assistant", "content": response})
                    unused_tools = all_tools - used_tools
                    unused_hint = f"（這些工具還未使用，推薦用一下他們: {', '.join(unused_tools)}）" if unused_tools else ""
                    messages.append({
                        "role": "user",
                        "content": REACT_INSUFFICIENT_TOOLS_MSG.format(
                            tool_calls_count=tool_calls_count,
                            min_tool_calls=min_tool_calls,
                            unused_hint=unused_hint,
                        ),
                    })
                    continue

                # 正常結束
                final_answer = response.split("Final Answer:")[-1].strip()
                logger.info(t('report.sectionGenDone', title=section.title, count=tool_calls_count))

                if self.report_logger:
                    self.report_logger.log_section_content(
                        section_title=section.title,
                        section_index=section_index,
                        content=final_answer,
                        tool_calls_count=tool_calls_count
                    )
                return final_answer

            # ── 情況2：LLM 嘗試調用工具 ──
            if has_tool_calls:
                # 工具額度已耗盡 → 明確告知，要求輸出 Final Answer
                if tool_calls_count >= self.MAX_TOOL_CALLS_PER_SECTION:
                    messages.append({"role": "assistant", "content": response})
                    messages.append({
                        "role": "user",
                        "content": REACT_TOOL_LIMIT_MSG.format(
                            tool_calls_count=tool_calls_count,
                            max_tool_calls=self.MAX_TOOL_CALLS_PER_SECTION,
                        ),
                    })
                    continue

                # 只執行第一個工具調用
                call = tool_calls[0]
                if len(tool_calls) > 1:
                    logger.info(t('report.multiToolOnlyFirst', total=len(tool_calls), toolName=call['name']))

                if self.report_logger:
                    self.report_logger.log_tool_call(
                        section_title=section.title,
                        section_index=section_index,
                        tool_name=call["name"],
                        parameters=call.get("parameters", {}),
                        iteration=iteration + 1
                    )

                result = self._execute_tool(
                    call["name"],
                    call.get("parameters", {}),
                    report_context=report_context
                )

                if self.report_logger:
                    self.report_logger.log_tool_result(
                        section_title=section.title,
                        section_index=section_index,
                        tool_name=call["name"],
                        result=result,
                        iteration=iteration + 1
                    )

                tool_calls_count += 1
                used_tools.add(call['name'])

                # 構建未使用工具提示
                unused_tools = all_tools - used_tools
                unused_hint = ""
                if unused_tools and tool_calls_count < self.MAX_TOOL_CALLS_PER_SECTION:
                    unused_hint = REACT_UNUSED_TOOLS_HINT.format(unused_list="、".join(unused_tools))

                messages.append({"role": "assistant", "content": response})
                messages.append({
                    "role": "user",
                    "content": REACT_OBSERVATION_TEMPLATE.format(
                        tool_name=call["name"],
                        result=result,
                        tool_calls_count=tool_calls_count,
                        max_tool_calls=self.MAX_TOOL_CALLS_PER_SECTION,
                        used_tools_str=", ".join(used_tools),
                        unused_hint=unused_hint,
                    ),
                })
                continue

            # ── 情況3：既沒有工具調用，也沒有 Final Answer ──
            messages.append({"role": "assistant", "content": response})

            if tool_calls_count < min_tool_calls:
                # 工具調用次數不足，推薦未用過的工具
                unused_tools = all_tools - used_tools
                unused_hint = f"（這些工具還未使用，推薦用一下他們: {', '.join(unused_tools)}）" if unused_tools else ""

                messages.append({
                    "role": "user",
                    "content": REACT_INSUFFICIENT_TOOLS_MSG_ALT.format(
                        tool_calls_count=tool_calls_count,
                        min_tool_calls=min_tool_calls,
                        unused_hint=unused_hint,
                    ),
                })
                continue

            # 工具調用已足夠，LLM 輸出了內容但沒帶 "Final Answer:" 前綴
            # 直接將這段內容作為最終答案，不再空轉
            logger.info(t('report.sectionNoPrefix', title=section.title, count=tool_calls_count))
            final_answer = response.strip()

            if self.report_logger:
                self.report_logger.log_section_content(
                    section_title=section.title,
                    section_index=section_index,
                    content=final_answer,
                    tool_calls_count=tool_calls_count
                )
            return final_answer
        
        # 達到最大迭代次數，強制生成內容
        logger.warning(t('report.sectionMaxIter', title=section.title))
        messages.append({"role": "user", "content": REACT_FORCE_FINAL_MSG})
        
        response = self.llm.chat(
            messages=messages,
            temperature=0.5,
            max_tokens=4096
        )

        # 檢查強制收尾時 LLM 返回是否為 None
        if response is None:
            logger.error(t('report.sectionForceFailed', title=section.title))
            final_answer = t('report.sectionGenFailedContent')
        elif "Final Answer:" in response:
            final_answer = response.split("Final Answer:")[-1].strip()
        else:
            final_answer = response
        
        # 記錄章節內容生成完成日誌
        if self.report_logger:
            self.report_logger.log_section_content(
                section_title=section.title,
                section_index=section_index,
                content=final_answer,
                tool_calls_count=tool_calls_count
            )
        
        return final_answer
    
    def generate_report(
        self, 
        progress_callback: Optional[Callable[[str, int, str], None]] = None,
        report_id: Optional[str] = None
    ) -> Report:
        """
        生成完整報告（分章節實時輸出）
        
        每個章節生成完成後立即保存到文件夾，不需要等待整個報告完成。
        文件結構：
        reports/{report_id}/
            meta.json       - 報告元信息
            outline.json    - 報告大綱
            progress.json   - 生成進度
            section_01.md   - 第1章節
            section_02.md   - 第2章節
            ...
            full_report.md  - 完整報告
        
        Args:
            progress_callback: 進度回調函數 (stage, progress, message)
            report_id: 報告ID（可選，如果不傳則自動生成）
            
        Returns:
            Report: 完整報告
        """
        import uuid
        
        # 如果沒有傳入 report_id，則自動生成
        if not report_id:
            report_id = f"report_{uuid.uuid4().hex[:12]}"
        start_time = datetime.now()
        
        report = Report(
            report_id=report_id,
            simulation_id=self.simulation_id,
            graph_id=self.graph_id,
            simulation_requirement=self.simulation_requirement,
            status=ReportStatus.PENDING,
            created_at=datetime.now().isoformat()
        )
        
        # 已完成的章節標題列表（用於進度追蹤）
        completed_section_titles = []
        
        try:
            # 初始化：創建報告文件夾並保存初始狀態
            ReportManager._ensure_report_folder(report_id)
            
            # 初始化日誌記錄器（結構化日誌 agent_log.jsonl）
            self.report_logger = ReportLogger(report_id)
            self.report_logger.log_start(
                simulation_id=self.simulation_id,
                graph_id=self.graph_id,
                simulation_requirement=self.simulation_requirement
            )
            
            # 初始化控制檯日誌記錄器（console_log.txt）
            self.console_logger = ReportConsoleLogger(report_id)
            
            ReportManager.update_progress(
                report_id, "pending", 0, t('progress.initReport'),
                completed_sections=[]
            )
            ReportManager.save_report(report)
            
            # 階段1: 規劃大綱
            report.status = ReportStatus.PLANNING
            ReportManager.update_progress(
                report_id, "planning", 5, t('progress.startPlanningOutline'),
                completed_sections=[]
            )
            
            # 記錄規劃開始日誌
            self.report_logger.log_planning_start()
            
            if progress_callback:
                progress_callback("planning", 0, t('progress.startPlanningOutline'))
            
            outline = self.plan_outline(
                progress_callback=lambda stage, prog, msg: 
                    progress_callback(stage, prog // 5, msg) if progress_callback else None
            )
            report.outline = outline
            
            # 記錄規劃完成日誌
            self.report_logger.log_planning_complete(outline.to_dict())
            
            # 保存大綱到文件
            ReportManager.save_outline(report_id, outline)
            ReportManager.update_progress(
                report_id, "planning", 15, t('progress.outlineDone', count=len(outline.sections)),
                completed_sections=[]
            )
            ReportManager.save_report(report)
            
            logger.info(t('report.outlineSavedToFile', reportId=report_id))
            
            # 階段2: 逐章節生成（分章節保存）
            report.status = ReportStatus.GENERATING
            
            total_sections = len(outline.sections)
            generated_sections = []  # 保存內容用於上下文
            
            for i, section in enumerate(outline.sections):
                section_num = i + 1
                base_progress = 20 + int((i / total_sections) * 70)
                
                # 更新進度
                ReportManager.update_progress(
                    report_id, "generating", base_progress,
                    t('progress.generatingSection', title=section.title, current=section_num, total=total_sections),
                    current_section=section.title,
                    completed_sections=completed_section_titles
                )

                if progress_callback:
                    progress_callback(
                        "generating",
                        base_progress,
                        t('progress.generatingSection', title=section.title, current=section_num, total=total_sections)
                    )
                
                # 生成主章節內容
                section_content = self._generate_section_react(
                    section=section,
                    outline=outline,
                    previous_sections=generated_sections,
                    progress_callback=lambda stage, prog, msg:
                        progress_callback(
                            stage, 
                            base_progress + int(prog * 0.7 / total_sections),
                            msg
                        ) if progress_callback else None,
                    section_index=section_num
                )
                
                section.content = section_content
                generated_sections.append(f"## {section.title}\n\n{section_content}")

                # 保存章節
                ReportManager.save_section(report_id, section_num, section)
                completed_section_titles.append(section.title)

                # 記錄章節完成日誌
                full_section_content = f"## {section.title}\n\n{section_content}"

                if self.report_logger:
                    self.report_logger.log_section_full_complete(
                        section_title=section.title,
                        section_index=section_num,
                        full_content=full_section_content.strip()
                    )

                logger.info(t('report.sectionSaved', reportId=report_id, sectionNum=f"{section_num:02d}"))
                
                # 更新進度
                ReportManager.update_progress(
                    report_id, "generating", 
                    base_progress + int(70 / total_sections),
                    t('progress.sectionDone', title=section.title),
                    current_section=None,
                    completed_sections=completed_section_titles
                )
            
            # 階段3: 組裝完整報告
            if progress_callback:
                progress_callback("generating", 95, t('progress.assemblingReport'))
            
            ReportManager.update_progress(
                report_id, "generating", 95, t('progress.assemblingReport'),
                completed_sections=completed_section_titles
            )
            
            # 使用ReportManager組裝完整報告
            report.markdown_content = ReportManager.assemble_full_report(report_id, outline)
            report.status = ReportStatus.COMPLETED
            report.completed_at = datetime.now().isoformat()
            
            # 計算總耗時
            total_time_seconds = (datetime.now() - start_time).total_seconds()
            
            # 記錄報告完成日誌
            if self.report_logger:
                self.report_logger.log_report_complete(
                    total_sections=total_sections,
                    total_time_seconds=total_time_seconds
                )
            
            # 保存最終報告
            ReportManager.save_report(report)
            ReportManager.update_progress(
                report_id, "completed", 100, t('progress.reportComplete'),
                completed_sections=completed_section_titles
            )
            
            if progress_callback:
                progress_callback("completed", 100, t('progress.reportComplete'))
            
            logger.info(t('report.reportGenDone', reportId=report_id))
            
            # 關閉控制檯日誌記錄器
            if self.console_logger:
                self.console_logger.close()
                self.console_logger = None
            
            return report
            
        except Exception as e:
            logger.error(t('report.reportGenFailed', error=str(e)))
            report.status = ReportStatus.FAILED
            report.error = str(e)
            
            # 記錄錯誤日誌
            if self.report_logger:
                self.report_logger.log_error(str(e), "failed")
            
            # 保存失敗狀態
            try:
                ReportManager.save_report(report)
                ReportManager.update_progress(
                    report_id, "failed", -1, t('progress.reportFailed', error=str(e)),
                    completed_sections=completed_section_titles
                )
            except Exception:
                pass  # 忽略保存失敗的錯誤
            
            # 關閉控制檯日誌記錄器
            if self.console_logger:
                self.console_logger.close()
                self.console_logger = None
            
            return report
    
    def chat(
        self, 
        message: str,
        chat_history: List[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        與Report Agent對話
        
        在對話中Agent可以自主調用檢索工具來回答問題
        
        Args:
            message: 用戶消息
            chat_history: 對話歷史
            
        Returns:
            {
                "response": "Agent回覆",
                "tool_calls": [調用的工具列表],
                "sources": [信息來源]
            }
        """
        logger.info(t('report.agentChat', message=message[:50]))
        
        chat_history = chat_history or []
        
        # 獲取已生成的報告內容
        report_content = ""
        try:
            report = ReportManager.get_report_by_simulation(self.simulation_id)
            if report and report.markdown_content:
                # 限制報告長度，避免上下文過長
                report_content = report.markdown_content[:15000]
                if len(report.markdown_content) > 15000:
                    report_content += "\n\n... [報告內容已截斷] ..."
        except Exception as e:
            logger.warning(t('report.fetchReportFailed', error=e))
        
        system_prompt = CHAT_SYSTEM_PROMPT_TEMPLATE.format(
            simulation_requirement=self.simulation_requirement,
            report_content=report_content if report_content else "（暫無報告）",
            tools_description=self._get_tools_description(),
        )
        system_prompt = f"{system_prompt}\n\n{get_language_instruction()}"

        # 構建消息
        messages = [{"role": "system", "content": system_prompt}]
        
        # 添加歷史對話
        for h in chat_history[-10:]:  # 限制歷史長度
            messages.append(h)
        
        # 添加用戶消息
        messages.append({
            "role": "user", 
            "content": message
        })
        
        # ReACT循環（簡化版）
        tool_calls_made = []
        max_iterations = 2  # 減少迭代輪數
        
        for iteration in range(max_iterations):
            response = self.llm.chat(
                messages=messages,
                temperature=0.5
            )
            
            # 解析工具調用
            tool_calls = self._parse_tool_calls(response)
            
            if not tool_calls:
                # 沒有工具調用，直接返回響應
                clean_response = re.sub(r'<tool_call>.*?</tool_call>', '', response, flags=re.DOTALL)
                clean_response = re.sub(r'\[TOOL_CALL\].*?\)', '', clean_response)
                
                return {
                    "response": clean_response.strip(),
                    "tool_calls": tool_calls_made,
                    "sources": [tc.get("parameters", {}).get("query", "") for tc in tool_calls_made]
                }
            
            # 執行工具調用（限制數量）
            tool_results = []
            for call in tool_calls[:1]:  # 每輪最多執行1次工具調用
                if len(tool_calls_made) >= self.MAX_TOOL_CALLS_PER_CHAT:
                    break
                result = self._execute_tool(call["name"], call.get("parameters", {}))
                tool_results.append({
                    "tool": call["name"],
                    "result": result[:1500]  # 限制結果長度
                })
                tool_calls_made.append(call)
            
            # 將結果添加到消息
            messages.append({"role": "assistant", "content": response})
            observation = "\n".join([f"[{r['tool']}結果]\n{r['result']}" for r in tool_results])
            messages.append({
                "role": "user",
                "content": observation + CHAT_OBSERVATION_SUFFIX
            })
        
        # 達到最大迭代，獲取最終響應
        final_response = self.llm.chat(
            messages=messages,
            temperature=0.5
        )
        
        # 清理響應
        clean_response = re.sub(r'<tool_call>.*?</tool_call>', '', final_response, flags=re.DOTALL)
        clean_response = re.sub(r'\[TOOL_CALL\].*?\)', '', clean_response)
        
        return {
            "response": clean_response.strip(),
            "tool_calls": tool_calls_made,
            "sources": [tc.get("parameters", {}).get("query", "") for tc in tool_calls_made]
        }


class ReportManager:
    """
    報告管理器
    
    負責報告的持久化存儲和檢索
    
    文件結構（分章節輸出）：
    reports/
      {report_id}/
        meta.json          - 報告元信息和狀態
        outline.json       - 報告大綱
        progress.json      - 生成進度
        section_01.md      - 第1章節
        section_02.md      - 第2章節
        ...
        full_report.md     - 完整報告
    """
    
    # 報告存儲目錄
    REPORTS_DIR = os.path.join(Config.UPLOAD_FOLDER, 'reports')
    
    @classmethod
    def _ensure_reports_dir(cls):
        """確保報告根目錄存在"""
        os.makedirs(cls.REPORTS_DIR, exist_ok=True)
    
    @classmethod
    def _get_report_folder(cls, report_id: str) -> str:
        """獲取報告文件夾路徑"""
        return os.path.join(cls.REPORTS_DIR, report_id)
    
    @classmethod
    def _ensure_report_folder(cls, report_id: str) -> str:
        """確保報告文件夾存在並返回路徑"""
        folder = cls._get_report_folder(report_id)
        os.makedirs(folder, exist_ok=True)
        return folder
    
    @classmethod
    def _get_report_path(cls, report_id: str) -> str:
        """獲取報告元信息文件路徑"""
        return os.path.join(cls._get_report_folder(report_id), "meta.json")
    
    @classmethod
    def _get_report_markdown_path(cls, report_id: str) -> str:
        """獲取完整報告Markdown文件路徑"""
        return os.path.join(cls._get_report_folder(report_id), "full_report.md")
    
    @classmethod
    def _get_outline_path(cls, report_id: str) -> str:
        """獲取大綱文件路徑"""
        return os.path.join(cls._get_report_folder(report_id), "outline.json")
    
    @classmethod
    def _get_progress_path(cls, report_id: str) -> str:
        """獲取進度文件路徑"""
        return os.path.join(cls._get_report_folder(report_id), "progress.json")
    
    @classmethod
    def _get_section_path(cls, report_id: str, section_index: int) -> str:
        """獲取章節Markdown文件路徑"""
        return os.path.join(cls._get_report_folder(report_id), f"section_{section_index:02d}.md")
    
    @classmethod
    def _get_agent_log_path(cls, report_id: str) -> str:
        """獲取 Agent 日誌文件路徑"""
        return os.path.join(cls._get_report_folder(report_id), "agent_log.jsonl")
    
    @classmethod
    def _get_console_log_path(cls, report_id: str) -> str:
        """獲取控制檯日誌文件路徑"""
        return os.path.join(cls._get_report_folder(report_id), "console_log.txt")
    
    @classmethod
    def get_console_log(cls, report_id: str, from_line: int = 0) -> Dict[str, Any]:
        """
        獲取控制檯日誌內容
        
        這是報告生成過程中的控制檯輸出日誌（INFO、WARNING等），
        與 agent_log.jsonl 的結構化日誌不同。
        
        Args:
            report_id: 報告ID
            from_line: 從第幾行開始讀取（用於增量獲取，0 表示從頭開始）
            
        Returns:
            {
                "logs": [日誌行列表],
                "total_lines": 總行數,
                "from_line": 起始行號,
                "has_more": 是否還有更多日誌
            }
        """
        log_path = cls._get_console_log_path(report_id)
        
        if not os.path.exists(log_path):
            return {
                "logs": [],
                "total_lines": 0,
                "from_line": 0,
                "has_more": False
            }
        
        logs = []
        total_lines = 0
        
        with open(log_path, 'r', encoding='utf-8') as f:
            for i, line in enumerate(f):
                total_lines = i + 1
                if i >= from_line:
                    # 保留原始日誌行，去掉末尾換行符
                    logs.append(line.rstrip('\n\r'))
        
        return {
            "logs": logs,
            "total_lines": total_lines,
            "from_line": from_line,
            "has_more": False  # 已讀取到末尾
        }
    
    @classmethod
    def get_console_log_stream(cls, report_id: str) -> List[str]:
        """
        獲取完整的控制檯日誌（一次性獲取全部）
        
        Args:
            report_id: 報告ID
            
        Returns:
            日誌行列表
        """
        result = cls.get_console_log(report_id, from_line=0)
        return result["logs"]
    
    @classmethod
    def get_agent_log(cls, report_id: str, from_line: int = 0) -> Dict[str, Any]:
        """
        獲取 Agent 日誌內容
        
        Args:
            report_id: 報告ID
            from_line: 從第幾行開始讀取（用於增量獲取，0 表示從頭開始）
            
        Returns:
            {
                "logs": [日誌條目列表],
                "total_lines": 總行數,
                "from_line": 起始行號,
                "has_more": 是否還有更多日誌
            }
        """
        log_path = cls._get_agent_log_path(report_id)
        
        if not os.path.exists(log_path):
            return {
                "logs": [],
                "total_lines": 0,
                "from_line": 0,
                "has_more": False
            }
        
        logs = []
        total_lines = 0
        
        with open(log_path, 'r', encoding='utf-8') as f:
            for i, line in enumerate(f):
                total_lines = i + 1
                if i >= from_line:
                    try:
                        log_entry = json.loads(line.strip())
                        logs.append(log_entry)
                    except json.JSONDecodeError:
                        # 跳過解析失敗的行
                        continue
        
        return {
            "logs": logs,
            "total_lines": total_lines,
            "from_line": from_line,
            "has_more": False  # 已讀取到末尾
        }
    
    @classmethod
    def get_agent_log_stream(cls, report_id: str) -> List[Dict[str, Any]]:
        """
        獲取完整的 Agent 日誌（用於一次性獲取全部）
        
        Args:
            report_id: 報告ID
            
        Returns:
            日誌條目列表
        """
        result = cls.get_agent_log(report_id, from_line=0)
        return result["logs"]
    
    @classmethod
    def save_outline(cls, report_id: str, outline: ReportOutline) -> None:
        """
        保存報告大綱
        
        在規劃階段完成後立即調用
        """
        cls._ensure_report_folder(report_id)
        
        with open(cls._get_outline_path(report_id), 'w', encoding='utf-8') as f:
            json.dump(outline.to_dict(), f, ensure_ascii=False, indent=2)
        
        logger.info(t('report.outlineSaved', reportId=report_id))
    
    @classmethod
    def save_section(
        cls,
        report_id: str,
        section_index: int,
        section: ReportSection
    ) -> str:
        """
        保存單個章節

        在每個章節生成完成後立即調用，實現分章節輸出

        Args:
            report_id: 報告ID
            section_index: 章節索引（從1開始）
            section: 章節對象

        Returns:
            保存的文件路徑
        """
        cls._ensure_report_folder(report_id)

        # 構建章節Markdown內容 - 清理可能存在的重複標題
        cleaned_content = cls._clean_section_content(section.content, section.title)
        md_content = f"## {section.title}\n\n"
        if cleaned_content:
            md_content += f"{cleaned_content}\n\n"

        # 保存文件
        file_suffix = f"section_{section_index:02d}.md"
        file_path = os.path.join(cls._get_report_folder(report_id), file_suffix)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(md_content)

        logger.info(t('report.sectionFileSaved', reportId=report_id, fileSuffix=file_suffix))
        return file_path
    
    @classmethod
    def _clean_section_content(cls, content: str, section_title: str) -> str:
        """
        清理章節內容
        
        1. 移除內容開頭與章節標題重複的Markdown標題行
        2. 將所有 ### 及以下級別的標題轉換為粗體文本
        
        Args:
            content: 原始內容
            section_title: 章節標題
            
        Returns:
            清理後的內容
        """
        import re
        
        if not content:
            return content
        
        content = content.strip()
        lines = content.split('\n')
        cleaned_lines = []
        skip_next_empty = False
        
        for i, line in enumerate(lines):
            stripped = line.strip()
            
            # 檢查是否是Markdown標題行
            heading_match = re.match(r'^(#{1,6})\s+(.+)$', stripped)
            
            if heading_match:
                level = len(heading_match.group(1))
                title_text = heading_match.group(2).strip()
                
                # 檢查是否是與章節標題重複的標題（跳過前5行內的重複）
                if i < 5:
                    if title_text == section_title or title_text.replace(' ', '') == section_title.replace(' ', ''):
                        skip_next_empty = True
                        continue
                
                # 將所有級別的標題（#, ##, ###, ####等）轉換為粗體
                # 因為章節標題由系統添加，內容中不應有任何標題
                cleaned_lines.append(f"**{title_text}**")
                cleaned_lines.append("")  # 添加空行
                continue
            
            # 如果上一行是被跳過的標題，且當前行為空，也跳過
            if skip_next_empty and stripped == '':
                skip_next_empty = False
                continue
            
            skip_next_empty = False
            cleaned_lines.append(line)
        
        # 移除開頭的空行
        while cleaned_lines and cleaned_lines[0].strip() == '':
            cleaned_lines.pop(0)
        
        # 移除開頭的分隔線
        while cleaned_lines and cleaned_lines[0].strip() in ['---', '***', '___']:
            cleaned_lines.pop(0)
            # 同時移除分隔線後的空行
            while cleaned_lines and cleaned_lines[0].strip() == '':
                cleaned_lines.pop(0)
        
        return '\n'.join(cleaned_lines)
    
    @classmethod
    def update_progress(
        cls, 
        report_id: str, 
        status: str, 
        progress: int, 
        message: str,
        current_section: str = None,
        completed_sections: List[str] = None
    ) -> None:
        """
        更新報告生成進度
        
        前端可以通過讀取progress.json獲取實時進度
        """
        cls._ensure_report_folder(report_id)
        
        progress_data = {
            "status": status,
            "progress": progress,
            "message": message,
            "current_section": current_section,
            "completed_sections": completed_sections or [],
            "updated_at": datetime.now().isoformat()
        }
        
        with open(cls._get_progress_path(report_id), 'w', encoding='utf-8') as f:
            json.dump(progress_data, f, ensure_ascii=False, indent=2)
    
    @classmethod
    def get_progress(cls, report_id: str) -> Optional[Dict[str, Any]]:
        """獲取報告生成進度"""
        path = cls._get_progress_path(report_id)
        
        if not os.path.exists(path):
            return None
        
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    @classmethod
    def get_generated_sections(cls, report_id: str) -> List[Dict[str, Any]]:
        """
        獲取已生成的章節列表
        
        返回所有已保存的章節文件信息
        """
        folder = cls._get_report_folder(report_id)
        
        if not os.path.exists(folder):
            return []
        
        sections = []
        for filename in sorted(os.listdir(folder)):
            if filename.startswith('section_') and filename.endswith('.md'):
                file_path = os.path.join(folder, filename)
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()

                # 從文件名解析章節索引
                parts = filename.replace('.md', '').split('_')
                section_index = int(parts[1])

                sections.append({
                    "filename": filename,
                    "section_index": section_index,
                    "content": content
                })

        return sections
    
    @classmethod
    def assemble_full_report(cls, report_id: str, outline: ReportOutline) -> str:
        """
        組裝完整報告
        
        從已保存的章節文件組裝完整報告，並進行標題清理
        """
        folder = cls._get_report_folder(report_id)
        
        # 構建報告頭部
        md_content = f"# {outline.title}\n\n"
        md_content += f"> {outline.summary}\n\n"
        md_content += f"---\n\n"
        
        # 按順序讀取所有章節文件
        sections = cls.get_generated_sections(report_id)
        for section_info in sections:
            md_content += section_info["content"]
        
        # 後處理：清理整個報告的標題問題
        md_content = cls._post_process_report(md_content, outline)
        
        # 保存完整報告
        full_path = cls._get_report_markdown_path(report_id)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(md_content)
        
        logger.info(t('report.fullReportAssembled', reportId=report_id))
        return md_content
    
    @classmethod
    def _post_process_report(cls, content: str, outline: ReportOutline) -> str:
        """
        後處理報告內容
        
        1. 移除重複的標題
        2. 保留報告主標題(#)和章節標題(##)，移除其他級別的標題(###, ####等)
        3. 清理多餘的空行和分隔線
        
        Args:
            content: 原始報告內容
            outline: 報告大綱
            
        Returns:
            處理後的內容
        """
        import re
        
        lines = content.split('\n')
        processed_lines = []
        prev_was_heading = False
        
        # 收集大綱中的所有章節標題
        section_titles = set()
        for section in outline.sections:
            section_titles.add(section.title)
        
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            
            # 檢查是否是標題行
            heading_match = re.match(r'^(#{1,6})\s+(.+)$', stripped)
            
            if heading_match:
                level = len(heading_match.group(1))
                title = heading_match.group(2).strip()
                
                # 檢查是否是重複標題（在連續5行內出現相同內容的標題）
                is_duplicate = False
                for j in range(max(0, len(processed_lines) - 5), len(processed_lines)):
                    prev_line = processed_lines[j].strip()
                    prev_match = re.match(r'^(#{1,6})\s+(.+)$', prev_line)
                    if prev_match:
                        prev_title = prev_match.group(2).strip()
                        if prev_title == title:
                            is_duplicate = True
                            break
                
                if is_duplicate:
                    # 跳過重複標題及其後的空行
                    i += 1
                    while i < len(lines) and lines[i].strip() == '':
                        i += 1
                    continue
                
                # 標題層級處理：
                # - # (level=1) 只保留報告主標題
                # - ## (level=2) 保留章節標題
                # - ### 及以下 (level>=3) 轉換為粗體文本
                
                if level == 1:
                    if title == outline.title:
                        # 保留報告主標題
                        processed_lines.append(line)
                        prev_was_heading = True
                    elif title in section_titles:
                        # 章節標題錯誤使用了#，修正為##
                        processed_lines.append(f"## {title}")
                        prev_was_heading = True
                    else:
                        # 其他一級標題轉為粗體
                        processed_lines.append(f"**{title}**")
                        processed_lines.append("")
                        prev_was_heading = False
                elif level == 2:
                    if title in section_titles or title == outline.title:
                        # 保留章節標題
                        processed_lines.append(line)
                        prev_was_heading = True
                    else:
                        # 非章節的二級標題轉為粗體
                        processed_lines.append(f"**{title}**")
                        processed_lines.append("")
                        prev_was_heading = False
                else:
                    # ### 及以下級別的標題轉換為粗體文本
                    processed_lines.append(f"**{title}**")
                    processed_lines.append("")
                    prev_was_heading = False
                
                i += 1
                continue
            
            elif stripped == '---' and prev_was_heading:
                # 跳過標題後緊跟的分隔線
                i += 1
                continue
            
            elif stripped == '' and prev_was_heading:
                # 標題後只保留一個空行
                if processed_lines and processed_lines[-1].strip() != '':
                    processed_lines.append(line)
                prev_was_heading = False
            
            else:
                processed_lines.append(line)
                prev_was_heading = False
            
            i += 1
        
        # 清理連續的多個空行（保留最多2個）
        result_lines = []
        empty_count = 0
        for line in processed_lines:
            if line.strip() == '':
                empty_count += 1
                if empty_count <= 2:
                    result_lines.append(line)
            else:
                empty_count = 0
                result_lines.append(line)
        
        return '\n'.join(result_lines)
    
    @classmethod
    def save_report(cls, report: Report) -> None:
        """保存報告元信息和完整報告"""
        cls._ensure_report_folder(report.report_id)
        
        # 保存元信息JSON
        with open(cls._get_report_path(report.report_id), 'w', encoding='utf-8') as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
        
        # 保存大綱
        if report.outline:
            cls.save_outline(report.report_id, report.outline)
        
        # 保存完整Markdown報告
        if report.markdown_content:
            with open(cls._get_report_markdown_path(report.report_id), 'w', encoding='utf-8') as f:
                f.write(report.markdown_content)
        
        logger.info(t('report.reportSaved', reportId=report.report_id))
    
    @classmethod
    def get_report(cls, report_id: str) -> Optional[Report]:
        """獲取報告"""
        path = cls._get_report_path(report_id)
        
        if not os.path.exists(path):
            # 兼容舊格式：檢查直接存儲在reports目錄下的文件
            old_path = os.path.join(cls.REPORTS_DIR, f"{report_id}.json")
            if os.path.exists(old_path):
                path = old_path
            else:
                return None
        
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 重建Report對象
        outline = None
        if data.get('outline'):
            outline_data = data['outline']
            sections = []
            for s in outline_data.get('sections', []):
                sections.append(ReportSection(
                    title=s['title'],
                    content=s.get('content', '')
                ))
            outline = ReportOutline(
                title=outline_data['title'],
                summary=outline_data['summary'],
                sections=sections
            )
        
        # 如果markdown_content為空，嘗試從full_report.md讀取
        markdown_content = data.get('markdown_content', '')
        if not markdown_content:
            full_report_path = cls._get_report_markdown_path(report_id)
            if os.path.exists(full_report_path):
                with open(full_report_path, 'r', encoding='utf-8') as f:
                    markdown_content = f.read()
        
        return Report(
            report_id=data['report_id'],
            simulation_id=data['simulation_id'],
            graph_id=data['graph_id'],
            simulation_requirement=data['simulation_requirement'],
            status=ReportStatus(data['status']),
            outline=outline,
            markdown_content=markdown_content,
            created_at=data.get('created_at', ''),
            completed_at=data.get('completed_at', ''),
            error=data.get('error')
        )
    
    @classmethod
    def get_report_by_simulation(cls, simulation_id: str) -> Optional[Report]:
        """根據模擬ID獲取報告"""
        cls._ensure_reports_dir()
        
        for item in os.listdir(cls.REPORTS_DIR):
            item_path = os.path.join(cls.REPORTS_DIR, item)
            # 新格式：文件夾
            if os.path.isdir(item_path):
                report = cls.get_report(item)
                if report and report.simulation_id == simulation_id:
                    return report
            # 兼容舊格式：JSON文件
            elif item.endswith('.json'):
                report_id = item[:-5]
                report = cls.get_report(report_id)
                if report and report.simulation_id == simulation_id:
                    return report
        
        return None
    
    @classmethod
    def list_reports(cls, simulation_id: Optional[str] = None, limit: int = 50) -> List[Report]:
        """列出報告"""
        cls._ensure_reports_dir()
        
        reports = []
        for item in os.listdir(cls.REPORTS_DIR):
            item_path = os.path.join(cls.REPORTS_DIR, item)
            # 新格式：文件夾
            if os.path.isdir(item_path):
                report = cls.get_report(item)
                if report:
                    if simulation_id is None or report.simulation_id == simulation_id:
                        reports.append(report)
            # 兼容舊格式：JSON文件
            elif item.endswith('.json'):
                report_id = item[:-5]
                report = cls.get_report(report_id)
                if report:
                    if simulation_id is None or report.simulation_id == simulation_id:
                        reports.append(report)
        
        # 按創建時間倒序
        reports.sort(key=lambda r: r.created_at, reverse=True)
        
        return reports[:limit]
    
    @classmethod
    def delete_report(cls, report_id: str) -> bool:
        """刪除報告（整個文件夾）"""
        import shutil
        
        folder_path = cls._get_report_folder(report_id)
        
        # 新格式：刪除整個文件夾
        if os.path.exists(folder_path) and os.path.isdir(folder_path):
            shutil.rmtree(folder_path)
            logger.info(t('report.reportFolderDeleted', reportId=report_id))
            return True
        
        # 兼容舊格式：刪除單獨的文件
        deleted = False
        old_json_path = os.path.join(cls.REPORTS_DIR, f"{report_id}.json")
        old_md_path = os.path.join(cls.REPORTS_DIR, f"{report_id}.md")
        
        if os.path.exists(old_json_path):
            os.remove(old_json_path)
            deleted = True
        if os.path.exists(old_md_path):
            os.remove(old_md_path)
            deleted = True
        
        return deleted
