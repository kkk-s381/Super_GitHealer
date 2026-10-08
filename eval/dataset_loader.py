# -*- coding: utf-8 -*-
"""
缺陷评测集加载模块 (Dataset Loader)
负责解析类 SWE-bench 的基准测试题目（包含 Issue 文本、基线仓库、复现单测指令）
"""
from typing import List, Dict, Any

class BenchmarkDatasetLoader:
    """评测数据集加载与样本校验器"""

    def __init__(self, dataset_path: str) -> None:
        """
        初始化评测集加载器
        :param dataset_path: 数据集 JSON/JSONL 文件的本地路径
        """
        ...

    def load_instances(self) -> List[Dict[str, Any]]:
        """
        加载并解析数据集题目列表
        :return: 包含 instance_id, repo, issue_description, test_command 的样本列表
        """
        ...

    def get_instance_by_id(self, instance_id: str) -> Dict[str, Any]:
        """
        根据题目 ID 单独提取单个评测案例
        :param instance_id: 题目唯一标识
        :return: 单个题目详情字典
        """
        ...
