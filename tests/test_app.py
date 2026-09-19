# -*- coding: utf-8 -*-
"""网页界面冒烟测试。

用 Streamlit 官方的 AppTest 真正跑一遍页面脚本，确保：
- 脚本本身没有异常
- 首页能渲染出查询控件
- 结构化筛选区能出结果

不需要浏览器，也不需要启动服务器。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("streamlit", reason="未安装 streamlit，跳过网页界面用例")

from streamlit.testing.v1 import AppTest  # noqa: E402

APP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pkdb", "app.py"
)


def _run():
    app = AppTest.from_file(APP_PATH, default_timeout=60)
    app.run()
    return app


def test_app_renders_without_exception():
    app = _run()
    assert not app.exception, "页面脚本抛异常：%s" % list(app.exception)
    # 页面主体必须存在
    assert app.markdown, "页面没有渲染出任何内容"


def test_app_exposes_query_controls():
    app = _run()
    assert app.text_input, "缺少问题输入框"
    assert app.radio, "缺少检索模式切换"
    assert app.selectbox, "缺少筛选下拉"


def test_app_shows_no_readiness_warning_when_ready():
    """库里向量齐了、Key 也配了，就不该再弹"未就绪 / 未配置"的横幅。"""
    from pkdb import config, db

    if not os.path.isfile(config.DB_PATH):
        pytest.skip("尚未建库，跳过")
    conn = db.open_db()
    coverage = db.vector_coverage(conn)
    conn.close()

    if not (coverage["ready"] and config.LLM_API_KEY):
        pytest.skip("当前环境（向量或 Key）尚未就绪，跳过")

    app = _run()
    assert not app.exception
    warnings = [item.value for item in app.info
                if "未就绪" in item.value or "未配置" in item.value]
    assert not warnings, "已就绪却仍显示告警：%s" % warnings


def test_app_has_no_structured_filter_section():
    """回归用例：结构化筛选区已按要求从网页移除。"""
    app = _run()
    assert not app.exception
    assert not any("结构化筛选" in item.value for item in app.markdown), \
        "结构化筛选区应当已被移除"


def test_app_has_no_stray_empty_card():
    """回归用例：不能再用 `st.markdown('<div ...>')` + `st.markdown('</div>')` 包控件。

    那种写法下 Streamlit 会把未闭合 div 自动补全，页面上凭空多出空卡片
    （表现为"输入框上方一个诡异的蓝色装饰框"）。
    """
    app = _run()
    assert not app.exception
    for item in app.markdown:
        source = item.value.strip()
        # 整块卡片必须一次输出：以 <div 开头就必须以 </div> 结尾
        if source.startswith('<div class="pk-card"'):
            assert source.endswith("</div>"), "pk-card 必须是自闭合的整块 HTML"
