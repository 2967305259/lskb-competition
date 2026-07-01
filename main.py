#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
冷水坑杯 #3 比赛管理系统 - 启动脚本（简化版）
如需自动安装依赖，请使用 run.py
"""

import os
import sys

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app import create_app

if __name__ == '__main__':
    env = os.environ.get('FLASK_ENV', 'development')
    app = create_app(env)
    print("=" * 60)
    print("  冷水坑杯 #3 比赛管理系统")
    print("=" * 60)
    print(f"  启动地址: http://0.0.0.0:19198")
    print(f"  默认管理员: admin / admin123")
    print("=" * 60)
    app.run(host='0.0.0.0', port=19198, debug=(env == 'development'))
