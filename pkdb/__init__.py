# -*- coding: utf-8 -*-
"""pkdb —— 个人简历数据库 + 文档 RAG 检索 + 大模型生成 原型。

分层：
    config          配置中心（路径 / 模型 / 参数）
    docx_reader     docx -> 段落序列
    chunker         段落 -> 父块 / 子块
    extract_profile 段落 -> 结构化字段
    db + schema     SQLite 读写
    llm/            供应商层（embedding / rerank / llm）
    retrieve        三路检索 + RRF 融合
    answer          编排：检索 -> 提示词 -> 生成 -> 引用
    cli / app       命令行 / 网页
"""

__version__ = "0.1.0"
