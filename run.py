#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
冷水坑杯 #3 报名管理系统 - 启动脚本
自动检测 Python 环境，安装依赖，启动服务
"""
import os, sys, subprocess

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

REQUIREMENTS = [
    "Flask==2.3.3",
    "Flask-SQLAlchemy==3.0.5",
    "Flask-Login==0.6.2",
    "Flask-WTF==1.1.1",
    "Flask-Migrate==4.0.5",
    "WTForms==3.0.1",
    "email-validator==2.0.0",
    "openpyxl==3.1.2",
    "Werkzeug==2.3.7",
]

FALLBACK_PYTHON = r"C:\Users\MSN\AppData\Local\Programs\Python\Python313\python.exe"


def _is_msys2():
    """检测当前 Python 是否为 MSYS2 版本（无法编译 C 扩展）"""
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import sys; print(sys.version)"],
            capture_output=True, text=True, timeout=5,
        )
        v = r.stdout.lower()
        return "msys" in v or "mingw" in v
    except Exception:
        return False


def _check_imports():
    """检查所有依赖是否可导入"""
    for pkg in REQUIREMENTS:
        name = pkg.split("==")[0].replace("-", "_").lower()
        try:
            __import__(name)
        except ImportError:
            return False
    return True


def _install(python_exe, packages):
    """用指定 Python 安装依赖，失败则退出"""
    for pkg in packages:
        print(f"  -> 安装 {pkg} ...")
        try:
            subprocess.check_call(
                [python_exe, "-m", "pip", "install", pkg, "--only-binary", ":all:"],
            )
        except subprocess.CalledProcessError:
            print(f"  -> 预编译包不可用，尝试源码安装 ...")
            subprocess.check_call(
                [python_exe, "-m", "pip", "install", pkg],
            )


# ==== 入口 ====
if __name__ == '__main__':
    # 如果是 MSYS2 Python，自动切换到 Python 3.13 重新执行
    if _is_msys2() and os.path.exists(FALLBACK_PYTHON) and sys.executable != FALLBACK_PYTHON:
        print(f"[run.py] 检测到 MSYS2 Python，自动切换到 Python 3.13 ...")
        os.execv(FALLBACK_PYTHON, [FALLBACK_PYTHON, __file__])

    # 安装缺失依赖
    if not _check_imports():
        print("============================================================")
        print("  检测到缺失依赖，正在自动安装...")
        print("============================================================")
        missing = []
        for pkg in REQUIREMENTS:
            name = pkg.split("==")[0].replace("-", "_").lower()
            try:
                __import__(name)
            except ImportError:
                missing.append(pkg)
        _install(sys.executable, missing)
        print("  依赖安装完成！")
        print("============================================================\n")

    from app import create_app

    env = os.environ.get('FLASK_ENV', 'development')
    app = create_app(env)
    print("============================================================")
    print("  冷水坑杯 #3 报名管理系统")
    print("============================================================")
    print(f"  启动地址: http://0.0.0.0:19198")
    print(f"  默认管理员: admin")
    print(f"  密码: admin123")
    print("============================================================")
    app.run(host='0.0.0.0', port=19198, debug=True)
