# -*- coding: utf-8 -*-
"""
全局配置模块
定义智能体默认模型、Docker 镜像、超时熔断阈值及最大重试配额
"""

class AgentConfig:
    """系统全局运行配置"""
    # LLM 推理配置
    DEFAULT_MODEL: str = "gpt-4o"
    TEMPERATURE: float = 0.0

    # Docker 沙箱执行配置
    DOCKER_BASE_IMAGE: str = "python:3.10-slim"
    SANDBOX_TIMEOUT_SECONDS: int = 60
    SANDBOX_MEMORY_LIMIT: str = "1g"

    # 循环状态机控制
    MAX_ITERATIONS: int = 5
    MAX_TRACEBACK_LINES: int = 40

    # 扫描与索引忽略目录
    IGNORE_DIRS: tuple = (
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".idea",
        ".pytest_cache",
    )
