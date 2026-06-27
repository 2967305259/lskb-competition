#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
冷水坑杯 #3 报名管理系统 - 启动脚本
"""

import os
import sys

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app import create_app

if __name__ == '__main__':
    # 获取环境配置
    env = os.environ.get('FLASK_ENV', 'development')
    
    # 创建应用
    app = create_app(env)
    
    # 启动应用
    app.run(debug=True, host='0.0.0.0', port=19198)
