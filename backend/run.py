"""
MiroFish Backend 啟動入口
"""

import os
import sys

# 解決 Windows 控制檯中文亂碼問題：在所有導入之前設置 UTF-8 編碼
if sys.platform == 'win32':
    # 設置環境變量確保 Python 使用 UTF-8
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    # 重新配置標準輸出流為 UTF-8
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# 添加項目根目錄到路徑
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.config import Config


def main():
    """主函數"""
    # 驗證配置
    errors = Config.validate()
    if errors:
        print("配置錯誤:")
        for err in errors:
            print(f"  - {err}")
        print("\n請檢查 .env 文件中的配置")
        sys.exit(1)
    
    # 創建應用
    app = create_app()
    
    # 獲取運行配置
    host = os.environ.get('FLASK_HOST', '0.0.0.0')
    port = int(os.environ.get('FLASK_PORT', 5001))
    debug = Config.DEBUG
    
    # 啟動服務
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == '__main__':
    main()

