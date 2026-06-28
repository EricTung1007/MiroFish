"""
OASIS 雙平臺並行模擬預設腳本
同時運行Twitter和Reddit模擬，讀取相同的配置文件

功能特性:
- 雙平臺（Twitter + Reddit）並行模擬
- 完成模擬後不立即關閉環境，進入等待命令模式
- 支持通過IPC接收Interview命令
- 支持單個Agent採訪和批量採訪
- 支持遠程關閉環境命令

使用方式:
    python run_parallel_simulation.py --config simulation_config.json
    python run_parallel_simulation.py --config simulation_config.json --no-wait  # 完成後立即關閉
    python run_parallel_simulation.py --config simulation_config.json --twitter-only
    python run_parallel_simulation.py --config simulation_config.json --reddit-only

日誌結構:
    sim_xxx/
    ├── twitter/
    │   └── actions.jsonl    # Twitter 平臺動作日誌
    ├── reddit/
    │   └── actions.jsonl    # Reddit 平臺動作日誌
    ├── simulation.log       # 主模擬進程日誌
    └── run_state.json       # 運行狀態（API 查詢用）
"""

# ============================================================
# 解決 Windows 編碼問題：在所有 import 之前設置 UTF-8 編碼
# 這是為了修復 OASIS 第三方庫讀取文件時未指定編碼的問題
# ============================================================
import sys
import os

if sys.platform == 'win32':
    # 設置 Python 默認 I/O 編碼為 UTF-8
    # 這會影響所有未指定編碼的 open() 調用
    os.environ.setdefault('PYTHONUTF8', '1')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    
    # 重新配置標準輸出流為 UTF-8（解決控制檯中文亂碼）
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    
    # 強制設置默認編碼（影響 open() 函數的默認編碼）
    # 注意：這需要在 Python 啟動時就設置，運行時設置可能不生效
    # 所以我們還需要 monkey-patch 內置的 open 函數
    import builtins
    _original_open = builtins.open
    
    def _utf8_open(file, mode='r', buffering=-1, encoding=None, errors=None, 
                   newline=None, closefd=True, opener=None):
        """
        包裝 open() 函數，對於文本模式默認使用 UTF-8 編碼
        這可以修復第三方庫（如 OASIS）讀取文件時未指定編碼的問題
        """
        # 只對文本模式（非二進制）且未指定編碼的情況設置默認編碼
        if encoding is None and 'b' not in mode:
            encoding = 'utf-8'
        return _original_open(file, mode, buffering, encoding, errors, 
                              newline, closefd, opener)
    
    builtins.open = _utf8_open

import argparse
import asyncio
import json
import logging
import multiprocessing
import random
import signal
import sqlite3
import warnings
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple


# 全局變量：用於信號處理
_shutdown_event = None
_cleanup_done = False

# 添加 backend 目錄到路徑
# 腳本固定位於 backend/scripts/ 目錄
_scripts_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.abspath(os.path.join(_scripts_dir, '..'))
_project_root = os.path.abspath(os.path.join(_backend_dir, '..'))
sys.path.insert(0, _scripts_dir)
sys.path.insert(0, _backend_dir)

# 加載項目根目錄的 .env 文件（包含 LLM_API_KEY 等配置）
from dotenv import load_dotenv
_env_file = os.path.join(_project_root, '.env')
if os.path.exists(_env_file):
    load_dotenv(_env_file)
    print(f"已加載環境配置: {_env_file}")
else:
    # 嘗試加載 backend/.env
    _backend_env = os.path.join(_backend_dir, '.env')
    if os.path.exists(_backend_env):
        load_dotenv(_backend_env)
        print(f"已加載環境配置: {_backend_env}")


class MaxTokensWarningFilter(logging.Filter):
    """過濾掉 camel-ai 關於 max_tokens 的警告（我們故意不設置 max_tokens，讓模型自行決定）"""
    
    def filter(self, record):
        # 過濾掉包含 max_tokens 警告的日誌
        if "max_tokens" in record.getMessage() and "Invalid or missing" in record.getMessage():
            return False
        return True


# 在模塊加載時立即添加過濾器，確保在 camel 代碼執行前生效
logging.getLogger().addFilter(MaxTokensWarningFilter())


def disable_oasis_logging():
    """
    禁用 OASIS 庫的詳細日誌輸出
    OASIS 的日誌太冗餘（記錄每個 agent 的觀察和動作），我們使用自己的 action_logger
    """
    # 禁用 OASIS 的所有日誌器
    oasis_loggers = [
        "social.agent",
        "social.twitter", 
        "social.rec",
        "oasis.env",
        "table",
    ]
    
    for logger_name in oasis_loggers:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.CRITICAL)  # 只記錄嚴重錯誤
        logger.handlers.clear()
        logger.propagate = False


def init_logging_for_simulation(simulation_dir: str):
    """
    初始化模擬的日誌配置
    
    Args:
        simulation_dir: 模擬目錄路徑
    """
    # 禁用 OASIS 的詳細日誌
    disable_oasis_logging()
    
    # 清理舊的 log 目錄（如果存在）
    old_log_dir = os.path.join(simulation_dir, "log")
    if os.path.exists(old_log_dir):
        import shutil
        shutil.rmtree(old_log_dir, ignore_errors=True)


from action_logger import SimulationLogManager, PlatformActionLogger

try:
    from camel.models import ModelFactory
    from camel.types import ModelPlatformType
    import oasis
    from oasis import (
        ActionType,
        LLMAction,
        ManualAction,
        generate_twitter_agent_graph,
        generate_reddit_agent_graph
    )
except ImportError as e:
    print(f"錯誤: 缺少依賴 {e}")
    print("請先安裝: pip install oasis-ai camel-ai")
    sys.exit(1)


# Twitter可用動作（不包含INTERVIEW，INTERVIEW只能通過ManualAction手動觸發）
TWITTER_ACTIONS = [
    ActionType.CREATE_POST,
    ActionType.LIKE_POST,
    ActionType.REPOST,
    ActionType.FOLLOW,
    ActionType.DO_NOTHING,
    ActionType.QUOTE_POST,
]

# Reddit可用動作（不包含INTERVIEW，INTERVIEW只能通過ManualAction手動觸發）
REDDIT_ACTIONS = [
    ActionType.LIKE_POST,
    ActionType.DISLIKE_POST,
    ActionType.CREATE_POST,
    ActionType.CREATE_COMMENT,
    ActionType.LIKE_COMMENT,
    ActionType.DISLIKE_COMMENT,
    ActionType.SEARCH_POSTS,
    ActionType.SEARCH_USER,
    ActionType.TREND,
    ActionType.REFRESH,
    ActionType.DO_NOTHING,
    ActionType.FOLLOW,
    ActionType.MUTE,
]


# IPC相關常量
IPC_COMMANDS_DIR = "ipc_commands"
IPC_RESPONSES_DIR = "ipc_responses"
ENV_STATUS_FILE = "env_status.json"

class CommandType:
    """命令類型常量"""
    INTERVIEW = "interview"
    BATCH_INTERVIEW = "batch_interview"
    CLOSE_ENV = "close_env"


class ParallelIPCHandler:
    """
    雙平臺IPC命令處理器
    
    管理兩個平臺的環境，處理Interview命令
    """
    
    def __init__(
        self,
        simulation_dir: str,
        twitter_env=None,
        twitter_agent_graph=None,
        reddit_env=None,
        reddit_agent_graph=None
    ):
        self.simulation_dir = simulation_dir
        self.twitter_env = twitter_env
        self.twitter_agent_graph = twitter_agent_graph
        self.reddit_env = reddit_env
        self.reddit_agent_graph = reddit_agent_graph
        
        self.commands_dir = os.path.join(simulation_dir, IPC_COMMANDS_DIR)
        self.responses_dir = os.path.join(simulation_dir, IPC_RESPONSES_DIR)
        self.status_file = os.path.join(simulation_dir, ENV_STATUS_FILE)
        
        # 確保目錄存在
        os.makedirs(self.commands_dir, exist_ok=True)
        os.makedirs(self.responses_dir, exist_ok=True)
    
    def update_status(self, status: str):
        """更新環境狀態"""
        with open(self.status_file, 'w', encoding='utf-8') as f:
            json.dump({
                "status": status,
                "twitter_available": self.twitter_env is not None,
                "reddit_available": self.reddit_env is not None,
                "timestamp": datetime.now().isoformat()
            }, f, ensure_ascii=False, indent=2)
    
    def poll_command(self) -> Optional[Dict[str, Any]]:
        """輪詢獲取待處理命令"""
        if not os.path.exists(self.commands_dir):
            return None
        
        # 獲取命令文件（按時間排序）
        command_files = []
        for filename in os.listdir(self.commands_dir):
            if filename.endswith('.json'):
                filepath = os.path.join(self.commands_dir, filename)
                command_files.append((filepath, os.path.getmtime(filepath)))
        
        command_files.sort(key=lambda x: x[1])
        
        for filepath, _ in command_files:
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
        
        return None
    
    def send_response(self, command_id: str, status: str, result: Dict = None, error: str = None):
        """發送響應"""
        response = {
            "command_id": command_id,
            "status": status,
            "result": result,
            "error": error,
            "timestamp": datetime.now().isoformat()
        }
        
        response_file = os.path.join(self.responses_dir, f"{command_id}.json")
        with open(response_file, 'w', encoding='utf-8') as f:
            json.dump(response, f, ensure_ascii=False, indent=2)
        
        # 刪除命令文件
        command_file = os.path.join(self.commands_dir, f"{command_id}.json")
        try:
            os.remove(command_file)
        except OSError:
            pass
    
    def _get_env_and_graph(self, platform: str):
        """
        獲取指定平臺的環境和agent_graph
        
        Args:
            platform: 平臺名稱 ("twitter" 或 "reddit")
            
        Returns:
            (env, agent_graph, platform_name) 或 (None, None, None)
        """
        if platform == "twitter" and self.twitter_env:
            return self.twitter_env, self.twitter_agent_graph, "twitter"
        elif platform == "reddit" and self.reddit_env:
            return self.reddit_env, self.reddit_agent_graph, "reddit"
        else:
            return None, None, None
    
    async def _interview_single_platform(self, agent_id: int, prompt: str, platform: str) -> Dict[str, Any]:
        """
        在單個平臺上執行Interview
        
        Returns:
            包含結果的字典，或包含error的字典
        """
        env, agent_graph, actual_platform = self._get_env_and_graph(platform)
        
        if not env or not agent_graph:
            return {"platform": platform, "error": f"{platform}平臺不可用"}
        
        try:
            agent = agent_graph.get_agent(agent_id)
            interview_action = ManualAction(
                action_type=ActionType.INTERVIEW,
                action_args={"prompt": prompt}
            )
            actions = {agent: interview_action}
            await env.step(actions)
            
            result = self._get_interview_result(agent_id, actual_platform)
            result["platform"] = actual_platform
            return result
            
        except Exception as e:
            return {"platform": platform, "error": str(e)}
    
    async def handle_interview(self, command_id: str, agent_id: int, prompt: str, platform: str = None) -> bool:
        """
        處理單個Agent採訪命令
        
        Args:
            command_id: 命令ID
            agent_id: Agent ID
            prompt: 採訪問題
            platform: 指定平臺（可選）
                - "twitter": 只採訪Twitter平臺
                - "reddit": 只採訪Reddit平臺
                - None/不指定: 同時採訪兩個平臺，返回整合結果
            
        Returns:
            True 表示成功，False 表示失敗
        """
        # 如果指定了平臺，只採訪該平臺
        if platform in ("twitter", "reddit"):
            result = await self._interview_single_platform(agent_id, prompt, platform)
            
            if "error" in result:
                self.send_response(command_id, "failed", error=result["error"])
                print(f"  Interview失敗: agent_id={agent_id}, platform={platform}, error={result['error']}")
                return False
            else:
                self.send_response(command_id, "completed", result=result)
                print(f"  Interview完成: agent_id={agent_id}, platform={platform}")
                return True
        
        # 未指定平臺：同時採訪兩個平臺
        if not self.twitter_env and not self.reddit_env:
            self.send_response(command_id, "failed", error="沒有可用的模擬環境")
            return False
        
        results = {
            "agent_id": agent_id,
            "prompt": prompt,
            "platforms": {}
        }
        success_count = 0
        
        # 並行採訪兩個平臺
        tasks = []
        platforms_to_interview = []
        
        if self.twitter_env:
            tasks.append(self._interview_single_platform(agent_id, prompt, "twitter"))
            platforms_to_interview.append("twitter")
        
        if self.reddit_env:
            tasks.append(self._interview_single_platform(agent_id, prompt, "reddit"))
            platforms_to_interview.append("reddit")
        
        # 並行執行
        platform_results = await asyncio.gather(*tasks)
        
        for platform_name, platform_result in zip(platforms_to_interview, platform_results):
            results["platforms"][platform_name] = platform_result
            if "error" not in platform_result:
                success_count += 1
        
        if success_count > 0:
            self.send_response(command_id, "completed", result=results)
            print(f"  Interview完成: agent_id={agent_id}, 成功平臺數={success_count}/{len(platforms_to_interview)}")
            return True
        else:
            errors = [f"{p}: {r.get('error', '未知錯誤')}" for p, r in results["platforms"].items()]
            self.send_response(command_id, "failed", error="; ".join(errors))
            print(f"  Interview失敗: agent_id={agent_id}, 所有平臺都失敗")
            return False
    
    async def handle_batch_interview(self, command_id: str, interviews: List[Dict], platform: str = None) -> bool:
        """
        處理批量採訪命令
        
        Args:
            command_id: 命令ID
            interviews: [{"agent_id": int, "prompt": str, "platform": str(optional)}, ...]
            platform: 默認平臺（可被每個interview項覆蓋）
                - "twitter": 只採訪Twitter平臺
                - "reddit": 只採訪Reddit平臺
                - None/不指定: 每個Agent同時採訪兩個平臺
        """
        # 按平臺分組
        twitter_interviews = []
        reddit_interviews = []
        both_platforms_interviews = []  # 需要同時採訪兩個平臺的
        
        for interview in interviews:
            item_platform = interview.get("platform", platform)
            if item_platform == "twitter":
                twitter_interviews.append(interview)
            elif item_platform == "reddit":
                reddit_interviews.append(interview)
            else:
                # 未指定平臺：兩個平臺都採訪
                both_platforms_interviews.append(interview)
        
        # 把 both_platforms_interviews 拆分到兩個平臺
        if both_platforms_interviews:
            if self.twitter_env:
                twitter_interviews.extend(both_platforms_interviews)
            if self.reddit_env:
                reddit_interviews.extend(both_platforms_interviews)
        
        results = {}
        
        # 處理Twitter平臺的採訪
        if twitter_interviews and self.twitter_env:
            try:
                twitter_actions = {}
                for interview in twitter_interviews:
                    agent_id = interview.get("agent_id")
                    prompt = interview.get("prompt", "")
                    try:
                        agent = self.twitter_agent_graph.get_agent(agent_id)
                        twitter_actions[agent] = ManualAction(
                            action_type=ActionType.INTERVIEW,
                            action_args={"prompt": prompt}
                        )
                    except Exception as e:
                        print(f"  警告: 無法獲取Twitter Agent {agent_id}: {e}")
                
                if twitter_actions:
                    await self.twitter_env.step(twitter_actions)
                    
                    for interview in twitter_interviews:
                        agent_id = interview.get("agent_id")
                        result = self._get_interview_result(agent_id, "twitter")
                        result["platform"] = "twitter"
                        results[f"twitter_{agent_id}"] = result
            except Exception as e:
                print(f"  Twitter批量Interview失敗: {e}")
        
        # 處理Reddit平臺的採訪
        if reddit_interviews and self.reddit_env:
            try:
                reddit_actions = {}
                for interview in reddit_interviews:
                    agent_id = interview.get("agent_id")
                    prompt = interview.get("prompt", "")
                    try:
                        agent = self.reddit_agent_graph.get_agent(agent_id)
                        reddit_actions[agent] = ManualAction(
                            action_type=ActionType.INTERVIEW,
                            action_args={"prompt": prompt}
                        )
                    except Exception as e:
                        print(f"  警告: 無法獲取Reddit Agent {agent_id}: {e}")
                
                if reddit_actions:
                    await self.reddit_env.step(reddit_actions)
                    
                    for interview in reddit_interviews:
                        agent_id = interview.get("agent_id")
                        result = self._get_interview_result(agent_id, "reddit")
                        result["platform"] = "reddit"
                        results[f"reddit_{agent_id}"] = result
            except Exception as e:
                print(f"  Reddit批量Interview失敗: {e}")
        
        if results:
            self.send_response(command_id, "completed", result={
                "interviews_count": len(results),
                "results": results
            })
            print(f"  批量Interview完成: {len(results)} 個Agent")
            return True
        else:
            self.send_response(command_id, "failed", error="沒有成功的採訪")
            return False
    
    def _get_interview_result(self, agent_id: int, platform: str) -> Dict[str, Any]:
        """從數據庫獲取最新的Interview結果"""
        db_path = os.path.join(self.simulation_dir, f"{platform}_simulation.db")
        
        result = {
            "agent_id": agent_id,
            "response": None,
            "timestamp": None
        }
        
        if not os.path.exists(db_path):
            return result
        
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # 查詢最新的Interview記錄
            cursor.execute("""
                SELECT user_id, info, created_at
                FROM trace
                WHERE action = ? AND user_id = ?
                ORDER BY created_at DESC
                LIMIT 1
            """, (ActionType.INTERVIEW.value, agent_id))
            
            row = cursor.fetchone()
            if row:
                user_id, info_json, created_at = row
                try:
                    info = json.loads(info_json) if info_json else {}
                    result["response"] = info.get("response", info)
                    result["timestamp"] = created_at
                except json.JSONDecodeError:
                    result["response"] = info_json
            
            conn.close()
            
        except Exception as e:
            print(f"  讀取Interview結果失敗: {e}")
        
        return result
    
    async def process_commands(self) -> bool:
        """
        處理所有待處理命令
        
        Returns:
            True 表示繼續運行，False 表示應該退出
        """
        command = self.poll_command()
        if not command:
            return True
        
        command_id = command.get("command_id")
        command_type = command.get("command_type")
        args = command.get("args", {})
        
        print(f"\n收到IPC命令: {command_type}, id={command_id}")
        
        if command_type == CommandType.INTERVIEW:
            await self.handle_interview(
                command_id,
                args.get("agent_id", 0),
                args.get("prompt", ""),
                args.get("platform")
            )
            return True
            
        elif command_type == CommandType.BATCH_INTERVIEW:
            await self.handle_batch_interview(
                command_id,
                args.get("interviews", []),
                args.get("platform")
            )
            return True
            
        elif command_type == CommandType.CLOSE_ENV:
            print("收到關閉環境命令")
            self.send_response(command_id, "completed", result={"message": "環境即將關閉"})
            return False
        
        else:
            self.send_response(command_id, "failed", error=f"未知命令類型: {command_type}")
            return True


def load_config(config_path: str) -> Dict[str, Any]:
    """加載配置文件"""
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


# 需要過濾掉的非核心動作類型（這些動作對分析價值較低）
FILTERED_ACTIONS = {'refresh', 'sign_up'}

# 動作類型映射表（數據庫中的名稱 -> 標準名稱）
ACTION_TYPE_MAP = {
    'create_post': 'CREATE_POST',
    'like_post': 'LIKE_POST',
    'dislike_post': 'DISLIKE_POST',
    'repost': 'REPOST',
    'quote_post': 'QUOTE_POST',
    'follow': 'FOLLOW',
    'mute': 'MUTE',
    'create_comment': 'CREATE_COMMENT',
    'like_comment': 'LIKE_COMMENT',
    'dislike_comment': 'DISLIKE_COMMENT',
    'search_posts': 'SEARCH_POSTS',
    'search_user': 'SEARCH_USER',
    'trend': 'TREND',
    'do_nothing': 'DO_NOTHING',
    'interview': 'INTERVIEW',
}


def get_agent_names_from_config(config: Dict[str, Any]) -> Dict[int, str]:
    """
    從 simulation_config 中獲取 agent_id -> entity_name 的映射
    
    這樣可以在 actions.jsonl 中顯示真實的實體名稱，而不是 "Agent_0" 這樣的代號
    
    Args:
        config: simulation_config.json 的內容
        
    Returns:
        agent_id -> entity_name 的映射字典
    """
    agent_names = {}
    agent_configs = config.get("agent_configs", [])
    
    for agent_config in agent_configs:
        agent_id = agent_config.get("agent_id")
        entity_name = agent_config.get("entity_name", f"Agent_{agent_id}")
        if agent_id is not None:
            agent_names[agent_id] = entity_name
    
    return agent_names


def fetch_new_actions_from_db(
    db_path: str,
    last_rowid: int,
    agent_names: Dict[int, str]
) -> Tuple[List[Dict[str, Any]], int]:
    """
    從數據庫中獲取新的動作記錄，並補充完整的上下文信息
    
    Args:
        db_path: 數據庫文件路徑
        last_rowid: 上次讀取的最大 rowid 值（使用 rowid 而不是 created_at，因為不同平臺的 created_at 格式不同）
        agent_names: agent_id -> agent_name 映射
        
    Returns:
        (actions_list, new_last_rowid)
        - actions_list: 動作列表，每個元素包含 agent_id, agent_name, action_type, action_args（含上下文信息）
        - new_last_rowid: 新的最大 rowid 值
    """
    actions = []
    new_last_rowid = last_rowid
    
    if not os.path.exists(db_path):
        return actions, new_last_rowid
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 使用 rowid 來追蹤已處理的記錄（rowid 是 SQLite 的內置自增字段）
        # 這樣可以避免 created_at 格式差異問題（Twitter 用整數，Reddit 用日期時間字符串）
        cursor.execute("""
            SELECT rowid, user_id, action, info
            FROM trace
            WHERE rowid > ?
            ORDER BY rowid ASC
        """, (last_rowid,))
        
        for rowid, user_id, action, info_json in cursor.fetchall():
            # 更新最大 rowid
            new_last_rowid = rowid
            
            # 過濾非核心動作
            if action in FILTERED_ACTIONS:
                continue
            
            # 解析動作參數
            try:
                action_args = json.loads(info_json) if info_json else {}
            except json.JSONDecodeError:
                action_args = {}
            
            # 精簡 action_args，只保留關鍵字段（保留完整內容，不截斷）
            simplified_args = {}
            if 'content' in action_args:
                simplified_args['content'] = action_args['content']
            if 'post_id' in action_args:
                simplified_args['post_id'] = action_args['post_id']
            if 'comment_id' in action_args:
                simplified_args['comment_id'] = action_args['comment_id']
            if 'quoted_id' in action_args:
                simplified_args['quoted_id'] = action_args['quoted_id']
            if 'new_post_id' in action_args:
                simplified_args['new_post_id'] = action_args['new_post_id']
            if 'follow_id' in action_args:
                simplified_args['follow_id'] = action_args['follow_id']
            if 'query' in action_args:
                simplified_args['query'] = action_args['query']
            if 'like_id' in action_args:
                simplified_args['like_id'] = action_args['like_id']
            if 'dislike_id' in action_args:
                simplified_args['dislike_id'] = action_args['dislike_id']
            
            # 轉換動作類型名稱
            action_type = ACTION_TYPE_MAP.get(action, action.upper())
            
            # 補充上下文信息（帖子內容、用戶名等）
            _enrich_action_context(cursor, action_type, simplified_args, agent_names)
            
            actions.append({
                'agent_id': user_id,
                'agent_name': agent_names.get(user_id, f'Agent_{user_id}'),
                'action_type': action_type,
                'action_args': simplified_args,
            })
        
        conn.close()
    except Exception as e:
        print(f"讀取數據庫動作失敗: {e}")
    
    return actions, new_last_rowid


def _enrich_action_context(
    cursor,
    action_type: str,
    action_args: Dict[str, Any],
    agent_names: Dict[int, str]
) -> None:
    """
    為動作補充上下文信息（帖子內容、用戶名等）
    
    Args:
        cursor: 數據庫遊標
        action_type: 動作類型
        action_args: 動作參數（會被修改）
        agent_names: agent_id -> agent_name 映射
    """
    try:
        # 點贊/踩帖子：補充帖子內容和作者
        if action_type in ('LIKE_POST', 'DISLIKE_POST'):
            post_id = action_args.get('post_id')
            if post_id:
                post_info = _get_post_info(cursor, post_id, agent_names)
                if post_info:
                    action_args['post_content'] = post_info.get('content', '')
                    action_args['post_author_name'] = post_info.get('author_name', '')
        
        # 轉發帖子：補充原帖內容和作者
        elif action_type == 'REPOST':
            new_post_id = action_args.get('new_post_id')
            if new_post_id:
                # 轉發帖子的 original_post_id 指向原帖
                cursor.execute("""
                    SELECT original_post_id FROM post WHERE post_id = ?
                """, (new_post_id,))
                row = cursor.fetchone()
                if row and row[0]:
                    original_post_id = row[0]
                    original_info = _get_post_info(cursor, original_post_id, agent_names)
                    if original_info:
                        action_args['original_content'] = original_info.get('content', '')
                        action_args['original_author_name'] = original_info.get('author_name', '')
        
        # 引用帖子：補充原帖內容、作者和引用評論
        elif action_type == 'QUOTE_POST':
            quoted_id = action_args.get('quoted_id')
            new_post_id = action_args.get('new_post_id')
            
            if quoted_id:
                original_info = _get_post_info(cursor, quoted_id, agent_names)
                if original_info:
                    action_args['original_content'] = original_info.get('content', '')
                    action_args['original_author_name'] = original_info.get('author_name', '')
            
            # 獲取引用帖子的評論內容（quote_content）
            if new_post_id:
                cursor.execute("""
                    SELECT quote_content FROM post WHERE post_id = ?
                """, (new_post_id,))
                row = cursor.fetchone()
                if row and row[0]:
                    action_args['quote_content'] = row[0]
        
        # 關注用戶：補充被關注用戶的名稱
        elif action_type == 'FOLLOW':
            follow_id = action_args.get('follow_id')
            if follow_id:
                # 從 follow 表獲取 followee_id
                cursor.execute("""
                    SELECT followee_id FROM follow WHERE follow_id = ?
                """, (follow_id,))
                row = cursor.fetchone()
                if row:
                    followee_id = row[0]
                    target_name = _get_user_name(cursor, followee_id, agent_names)
                    if target_name:
                        action_args['target_user_name'] = target_name
        
        # 屏蔽用戶：補充被屏蔽用戶的名稱
        elif action_type == 'MUTE':
            # 從 action_args 中獲取 user_id 或 target_id
            target_id = action_args.get('user_id') or action_args.get('target_id')
            if target_id:
                target_name = _get_user_name(cursor, target_id, agent_names)
                if target_name:
                    action_args['target_user_name'] = target_name
        
        # 點贊/踩評論：補充評論內容和作者
        elif action_type in ('LIKE_COMMENT', 'DISLIKE_COMMENT'):
            comment_id = action_args.get('comment_id')
            if comment_id:
                comment_info = _get_comment_info(cursor, comment_id, agent_names)
                if comment_info:
                    action_args['comment_content'] = comment_info.get('content', '')
                    action_args['comment_author_name'] = comment_info.get('author_name', '')
        
        # 發表評論：補充所評論的帖子信息
        elif action_type == 'CREATE_COMMENT':
            post_id = action_args.get('post_id')
            if post_id:
                post_info = _get_post_info(cursor, post_id, agent_names)
                if post_info:
                    action_args['post_content'] = post_info.get('content', '')
                    action_args['post_author_name'] = post_info.get('author_name', '')
    
    except Exception as e:
        # 補充上下文失敗不影響主流程
        print(f"補充動作上下文失敗: {e}")


def _get_post_info(
    cursor,
    post_id: int,
    agent_names: Dict[int, str]
) -> Optional[Dict[str, str]]:
    """
    獲取帖子信息
    
    Args:
        cursor: 數據庫遊標
        post_id: 帖子ID
        agent_names: agent_id -> agent_name 映射
        
    Returns:
        包含 content 和 author_name 的字典，或 None
    """
    try:
        cursor.execute("""
            SELECT p.content, p.user_id, u.agent_id
            FROM post p
            LEFT JOIN user u ON p.user_id = u.user_id
            WHERE p.post_id = ?
        """, (post_id,))
        row = cursor.fetchone()
        if row:
            content = row[0] or ''
            user_id = row[1]
            agent_id = row[2]
            
            # 優先使用 agent_names 中的名稱
            author_name = ''
            if agent_id is not None and agent_id in agent_names:
                author_name = agent_names[agent_id]
            elif user_id:
                # 從 user 表獲取名稱
                cursor.execute("SELECT name, user_name FROM user WHERE user_id = ?", (user_id,))
                user_row = cursor.fetchone()
                if user_row:
                    author_name = user_row[0] or user_row[1] or ''
            
            return {'content': content, 'author_name': author_name}
    except Exception:
        pass
    return None


def _get_user_name(
    cursor,
    user_id: int,
    agent_names: Dict[int, str]
) -> Optional[str]:
    """
    獲取用戶名稱
    
    Args:
        cursor: 數據庫遊標
        user_id: 用戶ID
        agent_names: agent_id -> agent_name 映射
        
    Returns:
        用戶名稱，或 None
    """
    try:
        cursor.execute("""
            SELECT agent_id, name, user_name FROM user WHERE user_id = ?
        """, (user_id,))
        row = cursor.fetchone()
        if row:
            agent_id = row[0]
            name = row[1]
            user_name = row[2]
            
            # 優先使用 agent_names 中的名稱
            if agent_id is not None and agent_id in agent_names:
                return agent_names[agent_id]
            return name or user_name or ''
    except Exception:
        pass
    return None


def _get_comment_info(
    cursor,
    comment_id: int,
    agent_names: Dict[int, str]
) -> Optional[Dict[str, str]]:
    """
    獲取評論信息
    
    Args:
        cursor: 數據庫遊標
        comment_id: 評論ID
        agent_names: agent_id -> agent_name 映射
        
    Returns:
        包含 content 和 author_name 的字典，或 None
    """
    try:
        cursor.execute("""
            SELECT c.content, c.user_id, u.agent_id
            FROM comment c
            LEFT JOIN user u ON c.user_id = u.user_id
            WHERE c.comment_id = ?
        """, (comment_id,))
        row = cursor.fetchone()
        if row:
            content = row[0] or ''
            user_id = row[1]
            agent_id = row[2]
            
            # 優先使用 agent_names 中的名稱
            author_name = ''
            if agent_id is not None and agent_id in agent_names:
                author_name = agent_names[agent_id]
            elif user_id:
                # 從 user 表獲取名稱
                cursor.execute("SELECT name, user_name FROM user WHERE user_id = ?", (user_id,))
                user_row = cursor.fetchone()
                if user_row:
                    author_name = user_row[0] or user_row[1] or ''
            
            return {'content': content, 'author_name': author_name}
    except Exception:
        pass
    return None


def create_model(config: Dict[str, Any], use_boost: bool = False):
    """
    創建LLM模型
    
    支持雙 LLM 配置，用於並行模擬時提速：
    - 通用配置：LLM_API_KEY, LLM_BASE_URL, LLM_MODEL_NAME
    - 加速配置（可選）：LLM_BOOST_API_KEY, LLM_BOOST_BASE_URL, LLM_BOOST_MODEL_NAME
    
    如果配置了加速 LLM，並行模擬時可以讓不同平臺使用不同的 API 服務商，提高併發能力。
    
    Args:
        config: 模擬配置字典
        use_boost: 是否使用加速 LLM 配置（如果可用）
    """
    # 檢查是否有加速配置
    boost_api_key = os.environ.get("LLM_BOOST_API_KEY", "")
    boost_base_url = os.environ.get("LLM_BOOST_BASE_URL", "")
    boost_model = os.environ.get("LLM_BOOST_MODEL_NAME", "")
    has_boost_config = bool(boost_api_key)
    
    # 根據參數和配置情況選擇使用哪個 LLM
    if use_boost and has_boost_config:
        # 使用加速配置
        llm_api_key = boost_api_key
        llm_base_url = boost_base_url
        llm_model = boost_model or os.environ.get("LLM_MODEL_NAME", "")
        config_label = "[加速LLM]"
    else:
        # 使用通用配置
        llm_api_key = os.environ.get("LLM_API_KEY", "")
        llm_base_url = os.environ.get("LLM_BASE_URL", "")
        llm_model = os.environ.get("LLM_MODEL_NAME", "")
        config_label = "[通用LLM]"
    
    # 如果 .env 中沒有模型名，則使用 config 作為備用
    if not llm_model:
        llm_model = config.get("llm_model", "gpt-4o-mini")
    
    # 設置 camel-ai 所需的環境變量
    if llm_api_key:
        os.environ["OPENAI_API_KEY"] = llm_api_key
    
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("缺少 API Key 配置，請在項目根目錄 .env 文件中設置 LLM_API_KEY")
    
    if llm_base_url:
        os.environ["OPENAI_API_BASE_URL"] = llm_base_url
    
    print(f"{config_label} model={llm_model}, base_url={llm_base_url[:40] if llm_base_url else '默認'}...")
    
    return ModelFactory.create(
        model_platform=ModelPlatformType.OPENAI,
        model_type=llm_model,
    )


def get_active_agents_for_round(
    env,
    config: Dict[str, Any],
    current_hour: int,
    round_num: int
) -> List:
    """根據時間和配置決定本輪激活哪些Agent"""
    time_config = config.get("time_config", {})
    agent_configs = config.get("agent_configs", [])
    
    base_min = time_config.get("agents_per_hour_min", 5)
    base_max = time_config.get("agents_per_hour_max", 20)
    
    peak_hours = time_config.get("peak_hours", [9, 10, 11, 14, 15, 20, 21, 22])
    off_peak_hours = time_config.get("off_peak_hours", [0, 1, 2, 3, 4, 5])
    
    if current_hour in peak_hours:
        multiplier = time_config.get("peak_activity_multiplier", 1.5)
    elif current_hour in off_peak_hours:
        multiplier = time_config.get("off_peak_activity_multiplier", 0.3)
    else:
        multiplier = 1.0
    
    target_count = int(random.uniform(base_min, base_max) * multiplier)
    
    candidates = []
    for cfg in agent_configs:
        agent_id = cfg.get("agent_id", 0)
        active_hours = cfg.get("active_hours", list(range(8, 23)))
        activity_level = cfg.get("activity_level", 0.5)
        
        if current_hour not in active_hours:
            continue
        
        if random.random() < activity_level:
            candidates.append(agent_id)
    
    selected_ids = random.sample(
        candidates, 
        min(target_count, len(candidates))
    ) if candidates else []
    
    active_agents = []
    for agent_id in selected_ids:
        try:
            agent = env.agent_graph.get_agent(agent_id)
            active_agents.append((agent_id, agent))
        except Exception:
            pass
    
    return active_agents


class PlatformSimulation:
    """平臺模擬結果容器"""
    def __init__(self):
        self.env = None
        self.agent_graph = None
        self.total_actions = 0


async def run_twitter_simulation(
    config: Dict[str, Any], 
    simulation_dir: str,
    action_logger: Optional[PlatformActionLogger] = None,
    main_logger: Optional[SimulationLogManager] = None,
    max_rounds: Optional[int] = None
) -> PlatformSimulation:
    """運行Twitter模擬
    
    Args:
        config: 模擬配置
        simulation_dir: 模擬目錄
        action_logger: 動作日誌記錄器
        main_logger: 主日誌管理器
        max_rounds: 最大模擬輪數（可選，用於截斷過長的模擬）
        
    Returns:
        PlatformSimulation: 包含env和agent_graph的結果對象
    """
    result = PlatformSimulation()
    
    def log_info(msg):
        if main_logger:
            main_logger.info(f"[Twitter] {msg}")
        print(f"[Twitter] {msg}")
    
    log_info("初始化...")
    
    # Twitter 使用通用 LLM 配置
    model = create_model(config, use_boost=False)
    
    # OASIS Twitter使用CSV格式
    profile_path = os.path.join(simulation_dir, "twitter_profiles.csv")
    if not os.path.exists(profile_path):
        log_info(f"錯誤: Profile文件不存在: {profile_path}")
        return result
    
    result.agent_graph = await generate_twitter_agent_graph(
        profile_path=profile_path,
        model=model,
        available_actions=TWITTER_ACTIONS,
    )
    
    # 從配置文件獲取 Agent 真實名稱映射（使用 entity_name 而非默認的 Agent_X）
    agent_names = get_agent_names_from_config(config)
    # 如果配置中沒有某個 agent，則使用 OASIS 的默認名稱
    for agent_id, agent in result.agent_graph.get_agents():
        if agent_id not in agent_names:
            agent_names[agent_id] = getattr(agent, 'name', f'Agent_{agent_id}')
    
    db_path = os.path.join(simulation_dir, "twitter_simulation.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    
    result.env = oasis.make(
        agent_graph=result.agent_graph,
        platform=oasis.DefaultPlatformType.TWITTER,
        database_path=db_path,
        semaphore=30,  # 限制最大併發 LLM 請求數，防止 API 過載
    )
    
    await result.env.reset()
    log_info("環境已啟動")
    
    if action_logger:
        action_logger.log_simulation_start(config)
    
    total_actions = 0
    last_rowid = 0  # 跟蹤數據庫中最後處理的行號（使用 rowid 避免 created_at 格式差異）
    
    # 執行初始事件
    event_config = config.get("event_config", {})
    initial_posts = event_config.get("initial_posts", [])
    
    # 記錄 round 0 開始（初始事件階段）
    if action_logger:
        action_logger.log_round_start(0, 0)  # round 0, simulated_hour 0
    
    initial_action_count = 0
    if initial_posts:
        initial_actions = {}
        for post in initial_posts:
            agent_id = post.get("poster_agent_id", 0)
            content = post.get("content", "")
            try:
                agent = result.env.agent_graph.get_agent(agent_id)
                initial_actions[agent] = ManualAction(
                    action_type=ActionType.CREATE_POST,
                    action_args={"content": content}
                )
                
                if action_logger:
                    action_logger.log_action(
                        round_num=0,
                        agent_id=agent_id,
                        agent_name=agent_names.get(agent_id, f"Agent_{agent_id}"),
                        action_type="CREATE_POST",
                        action_args={"content": content}
                    )
                    total_actions += 1
                    initial_action_count += 1
            except Exception:
                pass
        
        if initial_actions:
            await result.env.step(initial_actions)
            log_info(f"已發佈 {len(initial_actions)} 條初始帖子")
    
    # 記錄 round 0 結束
    if action_logger:
        action_logger.log_round_end(0, initial_action_count)
    
    # 主模擬循環
    time_config = config.get("time_config", {})
    total_hours = time_config.get("total_simulation_hours", 72)
    minutes_per_round = time_config.get("minutes_per_round", 30)
    total_rounds = (total_hours * 60) // minutes_per_round
    
    # 如果指定了最大輪數，則截斷
    if max_rounds is not None and max_rounds > 0:
        original_rounds = total_rounds
        total_rounds = min(total_rounds, max_rounds)
        if total_rounds < original_rounds:
            log_info(f"輪數已截斷: {original_rounds} -> {total_rounds} (max_rounds={max_rounds})")
    
    start_time = datetime.now()
    
    for round_num in range(total_rounds):
        # 檢查是否收到退出信號
        if _shutdown_event and _shutdown_event.is_set():
            if main_logger:
                main_logger.info(f"收到退出信號，在第 {round_num + 1} 輪停止模擬")
            break
        
        simulated_minutes = round_num * minutes_per_round
        simulated_hour = (simulated_minutes // 60) % 24
        simulated_day = simulated_minutes // (60 * 24) + 1
        
        active_agents = get_active_agents_for_round(
            result.env, config, simulated_hour, round_num
        )
        
        # 無論是否有活躍agent，都記錄round開始
        if action_logger:
            action_logger.log_round_start(round_num + 1, simulated_hour)
        
        if not active_agents:
            # 沒有活躍agent時也記錄round結束（actions_count=0）
            if action_logger:
                action_logger.log_round_end(round_num + 1, 0)
            continue
        
        actions = {agent: LLMAction() for _, agent in active_agents}
        await result.env.step(actions)
        
        # 從數據庫獲取實際執行的動作並記錄
        actual_actions, last_rowid = fetch_new_actions_from_db(
            db_path, last_rowid, agent_names
        )
        
        round_action_count = 0
        for action_data in actual_actions:
            if action_logger:
                action_logger.log_action(
                    round_num=round_num + 1,
                    agent_id=action_data['agent_id'],
                    agent_name=action_data['agent_name'],
                    action_type=action_data['action_type'],
                    action_args=action_data['action_args']
                )
                total_actions += 1
                round_action_count += 1
        
        if action_logger:
            action_logger.log_round_end(round_num + 1, round_action_count)
        
        if (round_num + 1) % 20 == 0:
            progress = (round_num + 1) / total_rounds * 100
            log_info(f"Day {simulated_day}, {simulated_hour:02d}:00 - Round {round_num + 1}/{total_rounds} ({progress:.1f}%)")
    
    # 注意：不關閉環境，保留給Interview使用
    
    if action_logger:
        action_logger.log_simulation_end(total_rounds, total_actions)
    
    result.total_actions = total_actions
    elapsed = (datetime.now() - start_time).total_seconds()
    log_info(f"模擬循環完成! 耗時: {elapsed:.1f}秒, 總動作: {total_actions}")
    
    return result


async def run_reddit_simulation(
    config: Dict[str, Any], 
    simulation_dir: str,
    action_logger: Optional[PlatformActionLogger] = None,
    main_logger: Optional[SimulationLogManager] = None,
    max_rounds: Optional[int] = None
) -> PlatformSimulation:
    """運行Reddit模擬
    
    Args:
        config: 模擬配置
        simulation_dir: 模擬目錄
        action_logger: 動作日誌記錄器
        main_logger: 主日誌管理器
        max_rounds: 最大模擬輪數（可選，用於截斷過長的模擬）
        
    Returns:
        PlatformSimulation: 包含env和agent_graph的結果對象
    """
    result = PlatformSimulation()
    
    def log_info(msg):
        if main_logger:
            main_logger.info(f"[Reddit] {msg}")
        print(f"[Reddit] {msg}")
    
    log_info("初始化...")
    
    # Reddit 使用加速 LLM 配置（如果有的話，否則回退到通用配置）
    model = create_model(config, use_boost=True)
    
    profile_path = os.path.join(simulation_dir, "reddit_profiles.json")
    if not os.path.exists(profile_path):
        log_info(f"錯誤: Profile文件不存在: {profile_path}")
        return result
    
    result.agent_graph = await generate_reddit_agent_graph(
        profile_path=profile_path,
        model=model,
        available_actions=REDDIT_ACTIONS,
    )
    
    # 從配置文件獲取 Agent 真實名稱映射（使用 entity_name 而非默認的 Agent_X）
    agent_names = get_agent_names_from_config(config)
    # 如果配置中沒有某個 agent，則使用 OASIS 的默認名稱
    for agent_id, agent in result.agent_graph.get_agents():
        if agent_id not in agent_names:
            agent_names[agent_id] = getattr(agent, 'name', f'Agent_{agent_id}')
    
    db_path = os.path.join(simulation_dir, "reddit_simulation.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    
    result.env = oasis.make(
        agent_graph=result.agent_graph,
        platform=oasis.DefaultPlatformType.REDDIT,
        database_path=db_path,
        semaphore=30,  # 限制最大併發 LLM 請求數，防止 API 過載
    )
    
    await result.env.reset()
    log_info("環境已啟動")
    
    if action_logger:
        action_logger.log_simulation_start(config)
    
    total_actions = 0
    last_rowid = 0  # 跟蹤數據庫中最後處理的行號（使用 rowid 避免 created_at 格式差異）
    
    # 執行初始事件
    event_config = config.get("event_config", {})
    initial_posts = event_config.get("initial_posts", [])
    
    # 記錄 round 0 開始（初始事件階段）
    if action_logger:
        action_logger.log_round_start(0, 0)  # round 0, simulated_hour 0
    
    initial_action_count = 0
    if initial_posts:
        initial_actions = {}
        for post in initial_posts:
            agent_id = post.get("poster_agent_id", 0)
            content = post.get("content", "")
            try:
                agent = result.env.agent_graph.get_agent(agent_id)
                if agent in initial_actions:
                    if not isinstance(initial_actions[agent], list):
                        initial_actions[agent] = [initial_actions[agent]]
                    initial_actions[agent].append(ManualAction(
                        action_type=ActionType.CREATE_POST,
                        action_args={"content": content}
                    ))
                else:
                    initial_actions[agent] = ManualAction(
                        action_type=ActionType.CREATE_POST,
                        action_args={"content": content}
                    )
                
                if action_logger:
                    action_logger.log_action(
                        round_num=0,
                        agent_id=agent_id,
                        agent_name=agent_names.get(agent_id, f"Agent_{agent_id}"),
                        action_type="CREATE_POST",
                        action_args={"content": content}
                    )
                    total_actions += 1
                    initial_action_count += 1
            except Exception:
                pass
        
        if initial_actions:
            await result.env.step(initial_actions)
            log_info(f"已發佈 {len(initial_actions)} 條初始帖子")
    
    # 記錄 round 0 結束
    if action_logger:
        action_logger.log_round_end(0, initial_action_count)
    
    # 主模擬循環
    time_config = config.get("time_config", {})
    total_hours = time_config.get("total_simulation_hours", 72)
    minutes_per_round = time_config.get("minutes_per_round", 30)
    total_rounds = (total_hours * 60) // minutes_per_round
    
    # 如果指定了最大輪數，則截斷
    if max_rounds is not None and max_rounds > 0:
        original_rounds = total_rounds
        total_rounds = min(total_rounds, max_rounds)
        if total_rounds < original_rounds:
            log_info(f"輪數已截斷: {original_rounds} -> {total_rounds} (max_rounds={max_rounds})")
    
    start_time = datetime.now()
    
    for round_num in range(total_rounds):
        # 檢查是否收到退出信號
        if _shutdown_event and _shutdown_event.is_set():
            if main_logger:
                main_logger.info(f"收到退出信號，在第 {round_num + 1} 輪停止模擬")
            break
        
        simulated_minutes = round_num * minutes_per_round
        simulated_hour = (simulated_minutes // 60) % 24
        simulated_day = simulated_minutes // (60 * 24) + 1
        
        active_agents = get_active_agents_for_round(
            result.env, config, simulated_hour, round_num
        )
        
        # 無論是否有活躍agent，都記錄round開始
        if action_logger:
            action_logger.log_round_start(round_num + 1, simulated_hour)
        
        if not active_agents:
            # 沒有活躍agent時也記錄round結束（actions_count=0）
            if action_logger:
                action_logger.log_round_end(round_num + 1, 0)
            continue
        
        actions = {agent: LLMAction() for _, agent in active_agents}
        await result.env.step(actions)
        
        # 從數據庫獲取實際執行的動作並記錄
        actual_actions, last_rowid = fetch_new_actions_from_db(
            db_path, last_rowid, agent_names
        )
        
        round_action_count = 0
        for action_data in actual_actions:
            if action_logger:
                action_logger.log_action(
                    round_num=round_num + 1,
                    agent_id=action_data['agent_id'],
                    agent_name=action_data['agent_name'],
                    action_type=action_data['action_type'],
                    action_args=action_data['action_args']
                )
                total_actions += 1
                round_action_count += 1
        
        if action_logger:
            action_logger.log_round_end(round_num + 1, round_action_count)
        
        if (round_num + 1) % 20 == 0:
            progress = (round_num + 1) / total_rounds * 100
            log_info(f"Day {simulated_day}, {simulated_hour:02d}:00 - Round {round_num + 1}/{total_rounds} ({progress:.1f}%)")
    
    # 注意：不關閉環境，保留給Interview使用
    
    if action_logger:
        action_logger.log_simulation_end(total_rounds, total_actions)
    
    result.total_actions = total_actions
    elapsed = (datetime.now() - start_time).total_seconds()
    log_info(f"模擬循環完成! 耗時: {elapsed:.1f}秒, 總動作: {total_actions}")
    
    return result


async def main():
    parser = argparse.ArgumentParser(description='OASIS雙平臺並行模擬')
    parser.add_argument(
        '--config', 
        type=str, 
        required=True,
        help='配置文件路徑 (simulation_config.json)'
    )
    parser.add_argument(
        '--twitter-only',
        action='store_true',
        help='只運行Twitter模擬'
    )
    parser.add_argument(
        '--reddit-only',
        action='store_true',
        help='只運行Reddit模擬'
    )
    parser.add_argument(
        '--max-rounds',
        type=int,
        default=None,
        help='最大模擬輪數（可選，用於截斷過長的模擬）'
    )
    parser.add_argument(
        '--no-wait',
        action='store_true',
        default=False,
        help='模擬完成後立即關閉環境，不進入等待命令模式'
    )
    
    args = parser.parse_args()
    
    # 在 main 函數開始時創建 shutdown 事件，確保整個程序都能響應退出信號
    global _shutdown_event
    _shutdown_event = asyncio.Event()
    
    if not os.path.exists(args.config):
        print(f"錯誤: 配置文件不存在: {args.config}")
        sys.exit(1)
    
    config = load_config(args.config)
    simulation_dir = os.path.dirname(args.config) or "."
    wait_for_commands = not args.no_wait
    
    # 初始化日誌配置（禁用 OASIS 日誌，清理舊文件）
    init_logging_for_simulation(simulation_dir)
    
    # 創建日誌管理器
    log_manager = SimulationLogManager(simulation_dir)
    twitter_logger = log_manager.get_twitter_logger()
    reddit_logger = log_manager.get_reddit_logger()
    
    log_manager.info("=" * 60)
    log_manager.info("OASIS 雙平臺並行模擬")
    log_manager.info(f"配置文件: {args.config}")
    log_manager.info(f"模擬ID: {config.get('simulation_id', 'unknown')}")
    log_manager.info(f"等待命令模式: {'啟用' if wait_for_commands else '禁用'}")
    log_manager.info("=" * 60)
    
    time_config = config.get("time_config", {})
    total_hours = time_config.get('total_simulation_hours', 72)
    minutes_per_round = time_config.get('minutes_per_round', 30)
    config_total_rounds = (total_hours * 60) // minutes_per_round
    
    log_manager.info(f"模擬參數:")
    log_manager.info(f"  - 總模擬時長: {total_hours}小時")
    log_manager.info(f"  - 每輪時間: {minutes_per_round}分鐘")
    log_manager.info(f"  - 配置總輪數: {config_total_rounds}")
    if args.max_rounds:
        log_manager.info(f"  - 最大輪數限制: {args.max_rounds}")
        if args.max_rounds < config_total_rounds:
            log_manager.info(f"  - 實際執行輪數: {args.max_rounds} (已截斷)")
    log_manager.info(f"  - Agent數量: {len(config.get('agent_configs', []))}")
    
    log_manager.info("日誌結構:")
    log_manager.info(f"  - 主日誌: simulation.log")
    log_manager.info(f"  - Twitter動作: twitter/actions.jsonl")
    log_manager.info(f"  - Reddit動作: reddit/actions.jsonl")
    log_manager.info("=" * 60)
    
    start_time = datetime.now()
    
    # 存儲兩個平臺的模擬結果
    twitter_result: Optional[PlatformSimulation] = None
    reddit_result: Optional[PlatformSimulation] = None
    
    if args.twitter_only:
        twitter_result = await run_twitter_simulation(config, simulation_dir, twitter_logger, log_manager, args.max_rounds)
    elif args.reddit_only:
        reddit_result = await run_reddit_simulation(config, simulation_dir, reddit_logger, log_manager, args.max_rounds)
    else:
        # 並行運行（每個平臺使用獨立的日誌記錄器）
        results = await asyncio.gather(
            run_twitter_simulation(config, simulation_dir, twitter_logger, log_manager, args.max_rounds),
            run_reddit_simulation(config, simulation_dir, reddit_logger, log_manager, args.max_rounds),
        )
        twitter_result, reddit_result = results
    
    total_elapsed = (datetime.now() - start_time).total_seconds()
    log_manager.info("=" * 60)
    log_manager.info(f"模擬循環完成! 總耗時: {total_elapsed:.1f}秒")
    
    # 是否進入等待命令模式
    if wait_for_commands:
        log_manager.info("")
        log_manager.info("=" * 60)
        log_manager.info("進入等待命令模式 - 環境保持運行")
        log_manager.info("支持的命令: interview, batch_interview, close_env")
        log_manager.info("=" * 60)
        
        # 創建IPC處理器
        ipc_handler = ParallelIPCHandler(
            simulation_dir=simulation_dir,
            twitter_env=twitter_result.env if twitter_result else None,
            twitter_agent_graph=twitter_result.agent_graph if twitter_result else None,
            reddit_env=reddit_result.env if reddit_result else None,
            reddit_agent_graph=reddit_result.agent_graph if reddit_result else None
        )
        ipc_handler.update_status("alive")
        
        # 等待命令循環（使用全局 _shutdown_event）
        try:
            while not _shutdown_event.is_set():
                should_continue = await ipc_handler.process_commands()
                if not should_continue:
                    break
                # 使用 wait_for 替代 sleep，這樣可以響應 shutdown_event
                try:
                    await asyncio.wait_for(_shutdown_event.wait(), timeout=0.5)
                    break  # 收到退出信號
                except asyncio.TimeoutError:
                    pass  # 超時繼續循環
        except KeyboardInterrupt:
            print("\n收到中斷信號")
        except asyncio.CancelledError:
            print("\n任務被取消")
        except Exception as e:
            print(f"\n命令處理出錯: {e}")
        
        log_manager.info("\n關閉環境...")
        ipc_handler.update_status("stopped")
    
    # 關閉環境
    if twitter_result and twitter_result.env:
        await twitter_result.env.close()
        log_manager.info("[Twitter] 環境已關閉")
    
    if reddit_result and reddit_result.env:
        await reddit_result.env.close()
        log_manager.info("[Reddit] 環境已關閉")
    
    log_manager.info("=" * 60)
    log_manager.info(f"全部完成!")
    log_manager.info(f"日誌文件:")
    log_manager.info(f"  - {os.path.join(simulation_dir, 'simulation.log')}")
    log_manager.info(f"  - {os.path.join(simulation_dir, 'twitter', 'actions.jsonl')}")
    log_manager.info(f"  - {os.path.join(simulation_dir, 'reddit', 'actions.jsonl')}")
    log_manager.info("=" * 60)


def setup_signal_handlers(loop=None):
    """
    設置信號處理器，確保收到 SIGTERM/SIGINT 時能夠正確退出
    
    持久化模擬場景：模擬完成後不退出，等待 interview 命令
    當收到終止信號時，需要：
    1. 通知 asyncio 循環退出等待
    2. 讓程序有機會正常清理資源（關閉數據庫、環境等）
    3. 然後才退出
    """
    def signal_handler(signum, frame):
        global _cleanup_done
        sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        print(f"\n收到 {sig_name} 信號，正在退出...")
        
        if not _cleanup_done:
            _cleanup_done = True
            # 設置事件通知 asyncio 循環退出（讓循環有機會清理資源）
            if _shutdown_event:
                _shutdown_event.set()
        
        # 不要直接 sys.exit()，讓 asyncio 循環正常退出並清理資源
        # 如果是重複收到信號，才強制退出
        else:
            print("強制退出...")
            sys.exit(1)
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)


if __name__ == "__main__":
    setup_signal_handlers()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序被中斷")
    except SystemExit:
        pass
    finally:
        # 清理 multiprocessing 資源跟蹤器（防止退出時的警告）
        try:
            from multiprocessing import resource_tracker
            resource_tracker._resource_tracker._stop()
        except Exception:
            pass
        print("模擬進程已退出")
