#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
简化启动入口：等价于 `python run.py`（依赖检查 + 启动）。

保留本文件仅为兼容旧文档/旧部署脚本；启动逻辑集中在 run.py，避免启动横幅与
app.run 参数在多处重复。
"""

import os
import runpy
import sys

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

if __name__ == '__main__':
    sys.path.insert(0, BASE_DIR)
    os.chdir(BASE_DIR)
    runpy.run_path(os.path.join(BASE_DIR, 'run.py'), run_name='__main__')
