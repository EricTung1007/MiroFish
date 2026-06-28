"""
模擬IPC通信模塊
用於Flask後端和模擬腳本之間的進程間通信

通過文件系統實現簡單的命令/響應模式：
1. Flask寫入命令到 commands/ 目錄
2. 模擬腳本輪詢命令目錄，執行命令並寫入響應到 responses/ 目錄
3. Flask輪詢響應目錄獲取結果
"""

import os
import json
import time
import uuid
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..utils.logger import get_logger

logger = get_logger('mirofish.simulation_ipc')


class CommandType(str, Enum):
    """命令類型"""
    INTERVIEW = "interview"           # 單個Agent採訪
    BATCH_INTERVIEW = "batch_interview"  # 批量採訪
    CLOSE_ENV = "close_env"           # 關閉環境


class CommandStatus(str, Enum):
    """命令狀態"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class IPCCommand:
    """IPC命令"""
    command_id: str
    command_type: CommandType
    args: Dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": self.command_type.value,
            "args": self.args,
            "timestamp": self.timestamp
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'IPCCommand':
        return cls(
            command_id=data["command_id"],
            command_type=CommandType(data["command_type"]),
            args=data.get("args", {}),
            timestamp=data.get("timestamp", datetime.now().isoformat())
        )


@dataclass
class IPCResponse:
    """IPC響應"""
    command_id: str
    status: CommandStatus
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "timestamp": self.timestamp
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'IPCResponse':
        return cls(
            command_id=data["command_id"],
            status=CommandStatus(data["status"]),
            result=data.get("result"),
            error=data.get("error"),
            timestamp=data.get("timestamp", datetime.now().isoformat())
        )


class SimulationIPCClient:
    """
    模擬IPC客戶端（Flask端使用）
    
    用於向模擬進程發送命令並等待響應
    """
    
    def __init__(self, simulation_dir: str):
        """
        初始化IPC客戶端
        
        Args:
            simulation_dir: 模擬數據目錄
        """
        self.simulation_dir = simulation_dir
        self.commands_dir = os.path.join(simulation_dir, "ipc_commands")
        self.responses_dir = os.path.join(simulation_dir, "ipc_responses")
        
        # 確保目錄存在
        os.makedirs(self.commands_dir, exist_ok=True)
        os.makedirs(self.responses_dir, exist_ok=True)
    
    def send_command(
        self,
        command_type: CommandType,
        args: Dict[str, Any],
        timeout: float = 60.0,
        poll_interval: float = 0.5
    ) -> IPCResponse:
        """
        發送命令並等待響應
        
        Args:
            command_type: 命令類型
            args: 命令參數
            timeout: 超時時間（秒）
            poll_interval: 輪詢間隔（秒）
            
        Returns:
            IPCResponse
            
        Raises:
            TimeoutError: 等待響應超時
        """
        command_id = str(uuid.uuid4())
        command = IPCCommand(
            command_id=command_id,
            command_type=command_type,
            args=args
        )
        
        # 寫入命令文件
        command_file = os.path.join(self.commands_dir, f"{command_id}.json")
        with open(command_file, 'w', encoding='utf-8') as f:
            json.dump(command.to_dict(), f, ensure_ascii=False, indent=2)
        
        logger.info(f"發送IPC命令: {command_type.value}, command_id={command_id}")
        
        # 等待響應
        response_file = os.path.join(self.responses_dir, f"{command_id}.json")
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            if os.path.exists(response_file):
                try:
                    with open(response_file, 'r', encoding='utf-8') as f:
                        response_data = json.load(f)
                    response = IPCResponse.from_dict(response_data)
                    
                    # 清理命令和響應文件
                    try:
                        os.remove(command_file)
                        os.remove(response_file)
                    except OSError:
                        pass
                    
                    logger.info(f"收到IPC響應: command_id={command_id}, status={response.status.value}")
                    return response
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"解析響應失敗: {e}")
            
            time.sleep(poll_interval)
        
        # 超時
        logger.error(f"等待IPC響應超時: command_id={command_id}")
        
        # 清理命令文件
        try:
            os.remove(command_file)
        except OSError:
            pass
        
        raise TimeoutError(f"等待命令響應超時 ({timeout}秒)")
    
    def send_interview(
        self,
        agent_id: int,
        prompt: str,
        platform: str = None,
        timeout: float = 60.0
    ) -> IPCResponse:
        """
        發送單個Agent採訪命令
        
        Args:
            agent_id: Agent ID
            prompt: 採訪問題
            platform: 指定平臺（可選）
                - "twitter": 只採訪Twitter平臺
                - "reddit": 只採訪Reddit平臺  
                - None: 雙平臺模擬時同時採訪兩個平臺，單平臺模擬時採訪該平臺
            timeout: 超時時間
            
        Returns:
            IPCResponse，result字段包含採訪結果
        """
        args = {
            "agent_id": agent_id,
            "prompt": prompt
        }
        if platform:
            args["platform"] = platform
            
        return self.send_command(
            command_type=CommandType.INTERVIEW,
            args=args,
            timeout=timeout
        )
    
    def send_batch_interview(
        self,
        interviews: List[Dict[str, Any]],
        platform: str = None,
        timeout: float = 120.0
    ) -> IPCResponse:
        """
        發送批量採訪命令
        
        Args:
            interviews: 採訪列表，每個元素包含 {"agent_id": int, "prompt": str, "platform": str(可選)}
            platform: 默認平臺（可選，會被每個採訪項的platform覆蓋）
                - "twitter": 默認只採訪Twitter平臺
                - "reddit": 默認只採訪Reddit平臺
                - None: 雙平臺模擬時每個Agent同時採訪兩個平臺
            timeout: 超時時間
            
        Returns:
            IPCResponse，result字段包含所有采訪結果
        """
        args = {"interviews": interviews}
        if platform:
            args["platform"] = platform
            
        return self.send_command(
            command_type=CommandType.BATCH_INTERVIEW,
            args=args,
            timeout=timeout
        )
    
    def send_close_env(self, timeout: float = 30.0) -> IPCResponse:
        """
        發送關閉環境命令
        
        Args:
            timeout: 超時時間
            
        Returns:
            IPCResponse
        """
        return self.send_command(
            command_type=CommandType.CLOSE_ENV,
            args={},
            timeout=timeout
        )
    
    def check_env_alive(self) -> bool:
        """
        檢查模擬環境是否存活
        
        通過檢查 env_status.json 文件來判斷
        """
        status_file = os.path.join(self.simulation_dir, "env_status.json")
        if not os.path.exists(status_file):
            return False
        
        try:
            with open(status_file, 'r', encoding='utf-8') as f:
                status = json.load(f)
            return status.get("status") == "alive"
        except (json.JSONDecodeError, OSError):
            return False


class SimulationIPCServer:
    """
    模擬IPC服務器（模擬腳本端使用）
    
    輪詢命令目錄，執行命令並返回響應
    """
    
    def __init__(self, simulation_dir: str):
        """
        初始化IPC服務器
        
        Args:
            simulation_dir: 模擬數據目錄
        """
        self.simulation_dir = simulation_dir
        self.commands_dir = os.path.join(simulation_dir, "ipc_commands")
        self.responses_dir = os.path.join(simulation_dir, "ipc_responses")
        
        # 確保目錄存在
        os.makedirs(self.commands_dir, exist_ok=True)
        os.makedirs(self.responses_dir, exist_ok=True)
        
        # 環境狀態
        self._running = False
    
    def start(self):
        """標記服務器為運行狀態"""
        self._running = True
        self._update_env_status("alive")
    
    def stop(self):
        """標記服務器為停止狀態"""
        self._running = False
        self._update_env_status("stopped")
    
    def _update_env_status(self, status: str):
        """更新環境狀態文件"""
        status_file = os.path.join(self.simulation_dir, "env_status.json")
        with open(status_file, 'w', encoding='utf-8') as f:
            json.dump({
                "status": status,
                "timestamp": datetime.now().isoformat()
            }, f, ensure_ascii=False, indent=2)
    
    def poll_commands(self) -> Optional[IPCCommand]:
        """
        輪詢命令目錄，返回第一個待處理的命令
        
        Returns:
            IPCCommand 或 None
        """
        if not os.path.exists(self.commands_dir):
            return None
        
        # 按時間排序獲取命令文件
        command_files = []
        for filename in os.listdir(self.commands_dir):
            if filename.endswith('.json'):
                filepath = os.path.join(self.commands_dir, filename)
                command_files.append((filepath, os.path.getmtime(filepath)))
        
        command_files.sort(key=lambda x: x[1])
        
        for filepath, _ in command_files:
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return IPCCommand.from_dict(data)
            except (json.JSONDecodeError, KeyError, OSError) as e:
                logger.warning(f"讀取命令文件失敗: {filepath}, {e}")
                continue
        
        return None
    
    def send_response(self, response: IPCResponse):
        """
        發送響應
        
        Args:
            response: IPC響應
        """
        response_file = os.path.join(self.responses_dir, f"{response.command_id}.json")
        with open(response_file, 'w', encoding='utf-8') as f:
            json.dump(response.to_dict(), f, ensure_ascii=False, indent=2)
        
        # 刪除命令文件
        command_file = os.path.join(self.commands_dir, f"{response.command_id}.json")
        try:
            os.remove(command_file)
        except OSError:
            pass
    
    def send_success(self, command_id: str, result: Dict[str, Any]):
        """發送成功響應"""
        self.send_response(IPCResponse(
            command_id=command_id,
            status=CommandStatus.COMPLETED,
            result=result
        ))
    
    def send_error(self, command_id: str, error: str):
        """發送錯誤響應"""
        self.send_response(IPCResponse(
            command_id=command_id,
            status=CommandStatus.FAILED,
            error=error
        ))
