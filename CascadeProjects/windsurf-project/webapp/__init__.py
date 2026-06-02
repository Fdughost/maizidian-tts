"""Webapp 包初始化：在任何子模块读取环境变量前先加载 .env。"""
from __future__ import annotations

import os

try:
    from dotenv import load_dotenv

    _d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        env_path = os.path.join(_d, ".env")
        if os.path.exists(env_path):
            load_dotenv(env_path)
            break
        parent = os.path.dirname(_d)
        if parent == _d:
            break
        _d = parent
except ImportError:
    pass
