# -*- coding: utf-8 -*-
"""
批量基准评测运行器 (Benchmark Runner)
批量驱动智能体执行修复任务，统计 Pass@1 解决率、平均单题耗时与 Token 成本
"""
from typing import Dict, Any, List

class BenchmarkRunner:
    """批量量化评测套件执行器"""

    def __init__(self, dataset_path: str, output_report_path: str = "eval_report.json") -> None:
        """
        初始化评测运行器
        :param dataset_path: 评测题目数据集路径
        :param output_report_path: 评估汇总指标报告输出路径
        """
        ...

    def run_all(self, concurrency: int = 1) -> Dict[str, Any]:
        """
        全量执行评测流程并生成汇总报表
        :param concurrency: 并行运行的任务进程数
        :return: 包含总题数、修复成功数、Pass@1 百分比及平均耗时的结果字典
        """
        ...

    def evaluate_single_instance(self, instance: Dict[str, Any]) -> Dict[str, Any]:
        """
        针对单道题目运行 GitHealer 修复并判定成功与否
        :param instance: 单道缺陷题目数据
        :return: 该题目的运行指标与状态
        """
        ...

    def export_report(self, results: List[Dict[str, Any]]) -> None:
        """
        将评测明细与汇总指标持久化导出为 JSON 报告
        :param results: 全量题目执行日志与结果
        """
        ...
