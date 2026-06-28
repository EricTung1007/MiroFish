"""
配置管理
統一從項目根目錄的 .env 文件加載配置
"""

import os
from dotenv import load_dotenv

# 加載項目根目錄的 .env 文件
# 路徑: MiroFish/.env (相對於 backend/app/config.py)
project_root_env = os.path.join(os.path.dirname(__file__), '../../.env')

if os.path.exists(project_root_env):
    load_dotenv(project_root_env, override=True)
else:
    # 如果根目錄沒有 .env，嘗試加載環境變量（用於生產環境）
    load_dotenv(override=True)


class Config:
    """Flask配置類"""
    
    # Flask配置
    SECRET_KEY = os.environ.get('SECRET_KEY', 'mirofish-secret-key')
    DEBUG = os.environ.get('FLASK_DEBUG', 'True').lower() == 'true'
    
    # JSON配置 - 禁用ASCII轉義，讓中文直接顯示（而不是 \uXXXX 格式）
    JSON_AS_ASCII = False
    
    # LLM配置（統一使用OpenAI格式）
    LLM_API_KEY = os.environ.get('LLM_API_KEY', 'lm-studio')
    LLM_BASE_URL = os.environ.get('LLM_BASE_URL', 'http://localhost:1234/v1')
    LLM_MODEL_NAME = os.environ.get('LLM_MODEL_NAME', 'local-model')
    
    # Memory/graph backend. Use "local" to avoid Zep Cloud.
    MEMORY_BACKEND = os.environ.get('MEMORY_BACKEND', 'local').lower()

    # Zep配置
    ZEP_API_KEY = os.environ.get('ZEP_API_KEY')
    
    # 文件上傳配置
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '../uploads')
    ALLOWED_EXTENSIONS = {'pdf', 'md', 'txt', 'markdown'}
    
    # 文本處理配置
    DEFAULT_CHUNK_SIZE = 500  # 默認切塊大小
    DEFAULT_CHUNK_OVERLAP = 50  # 默認重疊大小
    
    # OASIS模擬配置
    OASIS_DEFAULT_MAX_ROUNDS = int(os.environ.get('OASIS_DEFAULT_MAX_ROUNDS', '10'))
    OASIS_SIMULATION_DATA_DIR = os.path.join(os.path.dirname(__file__), '../uploads/simulations')
    
    # OASIS平臺可用動作配置
    OASIS_TWITTER_ACTIONS = [
        'CREATE_POST', 'LIKE_POST', 'REPOST', 'FOLLOW', 'DO_NOTHING', 'QUOTE_POST'
    ]
    OASIS_REDDIT_ACTIONS = [
        'LIKE_POST', 'DISLIKE_POST', 'CREATE_POST', 'CREATE_COMMENT',
        'LIKE_COMMENT', 'DISLIKE_COMMENT', 'SEARCH_POSTS', 'SEARCH_USER',
        'TREND', 'REFRESH', 'DO_NOTHING', 'FOLLOW', 'MUTE'
    ]
    
    # Report Agent配置
    REPORT_AGENT_MAX_TOOL_CALLS = int(os.environ.get('REPORT_AGENT_MAX_TOOL_CALLS', '5'))
    REPORT_AGENT_MAX_REFLECTION_ROUNDS = int(os.environ.get('REPORT_AGENT_MAX_REFLECTION_ROUNDS', '2'))
    REPORT_AGENT_TEMPERATURE = float(os.environ.get('REPORT_AGENT_TEMPERATURE', '0.5'))
    
    @classmethod
    def validate(cls) -> list[str]:
        """驗證必要配置"""
        errors: list[str] = []
        if not cls.LLM_API_KEY:
            errors.append("LLM_API_KEY 未配置")
        if cls.MEMORY_BACKEND == 'zep' and not cls.ZEP_API_KEY:
            errors.append("ZEP_API_KEY 未配置")
        return errors
