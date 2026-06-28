"""
MiroFish Backend - Flask應用工廠
"""

import os
import warnings

# 抑制 multiprocessing resource_tracker 的警告（來自第三方庫如 transformers）
# 需要在所有其他導入之前設置
warnings.filterwarnings("ignore", message=".*resource_tracker.*")

from flask import Flask, request
from flask_cors import CORS

from .config import Config
from .utils.logger import setup_logger, get_logger


def create_app(config_class=Config):
    """Flask應用工廠函數"""
    app = Flask(__name__)
    app.config.from_object(config_class)
    
    # 設置JSON編碼：確保中文直接顯示（而不是 \uXXXX 格式）
    # Flask >= 2.3 使用 app.json.ensure_ascii，舊版本使用 JSON_AS_ASCII 配置
    if hasattr(app, 'json') and hasattr(app.json, 'ensure_ascii'):
        app.json.ensure_ascii = False
    
    # 設置日誌
    logger = setup_logger('mirofish')
    
    # 只在 reloader 子進程中打印啟動信息（避免 debug 模式下打印兩次）
    is_reloader_process = os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
    debug_mode = app.config.get('DEBUG', False)
    should_log_startup = not debug_mode or is_reloader_process
    
    if should_log_startup:
        logger.info("=" * 50)
        logger.info("MiroFish Backend 啟動中...")
        logger.info("=" * 50)
    
    # 啟用CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # 註冊模擬進程清理函數（確保服務器關閉時終止所有模擬進程）
    from .services.simulation_runner import SimulationRunner
    SimulationRunner.register_cleanup()
    if should_log_startup:
        logger.info("已註冊模擬進程清理函數")
    
    # 請求日誌中間件
    @app.before_request
    def log_request():
        logger = get_logger('mirofish.request')
        logger.debug(f"請求: {request.method} {request.path}")
        if request.content_type and 'json' in request.content_type:
            logger.debug(f"請求體: {request.get_json(silent=True)}")
    
    @app.after_request
    def log_response(response):
        logger = get_logger('mirofish.request')
        logger.debug(f"響應: {response.status_code}")
        return response
    
    # 註冊藍圖
    from .api import graph_bp, simulation_bp, report_bp
    app.register_blueprint(graph_bp, url_prefix='/api/graph')
    app.register_blueprint(simulation_bp, url_prefix='/api/simulation')
    app.register_blueprint(report_bp, url_prefix='/api/report')
    
    # 健康檢查
    @app.route('/health')
    def health():
        return {'status': 'ok', 'service': 'MiroFish Backend'}
    
    if should_log_startup:
        logger.info("MiroFish Backend 啟動完成")
    
    return app

