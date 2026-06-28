"""
日誌配置模塊
提供統一的日誌管理，同時輸出到控制檯和文件
"""

import os
import sys
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler


def _ensure_utf8_stdout():
    """
    確保 stdout/stderr 使用 UTF-8 編碼
    解決 Windows 控制檯中文亂碼問題
    """
    if sys.platform == 'win32':
        # Windows 下重新配置標準輸出為 UTF-8
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')


# 日誌目錄
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'logs')


def setup_logger(name: str = 'mirofish', level: int = logging.DEBUG) -> logging.Logger:
    """
    設置日誌器
    
    Args:
        name: 日誌器名稱
        level: 日誌級別
        
    Returns:
        配置好的日誌器
    """
    # 確保日誌目錄存在
    os.makedirs(LOG_DIR, exist_ok=True)
    
    # 創建日誌器
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # 阻止日誌向上傳播到根 logger，避免重複輸出
    logger.propagate = False
    
    # 如果已經有處理器，不重複添加
    if logger.handlers:
        return logger
    
    # 日誌格式
    detailed_formatter = logging.Formatter(
        '[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    simple_formatter = logging.Formatter(
        '[%(asctime)s] %(levelname)s: %(message)s',
        datefmt='%H:%M:%S'
    )
    
    # 1. 文件處理器 - 詳細日誌（按日期命名，帶輪轉）
    log_filename = datetime.now().strftime('%Y-%m-%d') + '.log'
    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, log_filename),
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(detailed_formatter)
    
    # 2. 控制檯處理器 - 簡潔日誌（INFO及以上）
    # 確保 Windows 下使用 UTF-8 編碼，避免中文亂碼
    _ensure_utf8_stdout()
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(simple_formatter)
    
    # 添加處理器
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger


def get_logger(name: str = 'mirofish') -> logging.Logger:
    """
    獲取日誌器（如果不存在則創建）
    
    Args:
        name: 日誌器名稱
        
    Returns:
        日誌器實例
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        return setup_logger(name)
    return logger


# 創建默認日誌器
logger = setup_logger()


# 便捷方法
def debug(msg: str, *args, **kwargs) -> None:
    logger.debug(msg, *args, **kwargs)

def info(msg: str, *args, **kwargs) -> None:
    logger.info(msg, *args, **kwargs)

def warning(msg: str, *args, **kwargs) -> None:
    logger.warning(msg, *args, **kwargs)

def error(msg: str, *args, **kwargs) -> None:
    logger.error(msg, *args, **kwargs)

def critical(msg: str, *args, **kwargs) -> None:
    logger.critical(msg, *args, **kwargs)

