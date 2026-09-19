# -*- coding: utf-8 -*-
"""pytest 夹具。

共用工具在 ``tests/helpers.py``（本文件只放夹具与路径引导）。

运行方式（在工作区根目录）：
    python -m pytest tests -q
"""

from __future__ import annotations

import os
import sys

import pytest

# 让 `pkdb` 与 `tests` 在任意工作目录下都可导入
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pkdb import db  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    """每个用例一个全新的临时库。"""
    connection = db.open_db(str(tmp_path / "test.sqlite3"))
    yield connection
    connection.close()
