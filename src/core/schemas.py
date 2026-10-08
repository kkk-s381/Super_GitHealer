# -*- coding: utf-8 -*-
"""
数据契约与状态定义模块 (Schemas & State Definition)
================================================================================
本模块定义了 GitHealer 系统中所有数据传输对象 (DTO)、Pydantic 校验契约模型，
以及驱动 LangGraph 状态机运行的核心全局状态 (AgentState)。

包含四大核心类族：
1. 语法检索契约: SymbolOutline
2. 代码编辑与版本契约: FileEditAction
3. 测试执行结果契约: TestResult
4. 深度反思与规划模型: PatchPlan, ReflexionReport, TrajectoryStep
5. 状态机全局状态与状态辅助函数: AgentState, create_initial_state, is_deadlock
"""

import os
import hashlib
import difflib
from typing import TypedDict, List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator


# =====================================================================
# 1. AST 语法大纲数据契约
# =====================================================================

class SymbolOutline(BaseModel):
    """
    AST 解析后的单个符号（类、函数、方法）骨架信息契约
    用于向 LLM 提供低 Token 占用的代码结构摘要，支持按需展开完整源码。
    """
    name: str = Field(..., description="符号名称（如 'calculate_loss' 或 'DataLoader'）")
    kind: Literal["class", "function", "method"] = Field(
        ..., description="符号类型，仅限 'class', 'function', 'method'"
    )
    start_line: int = Field(..., ge=1, description="代码起始行号（从 1 开始）")
    end_line: int = Field(..., ge=1, description="代码结束行号（从 1 开始）")
    docstring: Optional[str] = Field(default=None, description="符号对应的文档字符串摘要")

    @model_validator(mode="after")
    def validate_line_numbers(self) -> "SymbolOutline":
        """校验行号逻辑合法性：结束行号必须大于或等于起始行号"""
        if self.end_line < self.start_line:
            raise ValueError(
                f"行号区间非法: end_line ({self.end_line}) 必须大于等于 start_line ({self.start_line})"
            )
        return self

    @property
    def line_span(self) -> int:
        """计算该符号跨越的总行数"""
        return self.end_line - self.start_line + 1

    def to_compact_string(self) -> str:
        """
        转换为高密度紧凑格式，专供 LLM 上下文 Prompt 使用
        例如: '[function] parse_ast (L12-L45) - 提取 AST 骨架'
        """
        doc_snippet = f" - {self.docstring[:40]}..." if self.docstring else ""
        return f"[{self.kind}] {self.name} (L{self.start_line}-L{self.end_line}){doc_snippet}"

    def matches_query(self, query: str) -> bool:
        """检查符号名或文档是否匹配指定查询关键词（不区分大小写）"""
        q = query.lower()
        if q in self.name.lower():
            return True
        if self.docstring and q in self.docstring.lower():
            return True
        return False


# =====================================================================
# 2. 精准代码替换契约
# =====================================================================

class FileEditAction(BaseModel):
    """
    智能体发起的代码精确替换动作契约
    采用基于原始代码块的精准 Search-and-Replace，杜绝重写整文件导致的幻觉丢失。
    """
    file_path: str = Field(..., description="待修改的目标文件相对路径")
    old_str: str = Field(..., min_length=1, description="待替换的原始代码块（必须完全精确匹配）")
    new_str: str = Field(..., description="用于替换的新代码块")
    explanation: str = Field(..., description="本次修改的设计意图与修复原理说明")

    @field_validator("file_path", mode="before")
    @classmethod
    def normalize_file_path(cls, v: str) -> str:
        """规范化文件路径：统一为正斜杠，去除开头的 ./ 或 /，确保相对路径"""
        cleaned = v.strip().replace("\\", "/")
        if cleaned.startswith("./"):
            cleaned = cleaned[2:]
        elif cleaned.startswith("/"):
            cleaned = cleaned[1:]
        if not cleaned:
            raise ValueError("file_path 不能为空路径")
        return cleaned

    @model_validator(mode="after")
    def validate_code_change(self) -> "FileEditAction":
        """校验替换内容：old_str 与 new_str 不能完全一致，否则为无效修改"""
        if self.old_str == self.new_str:
            raise ValueError("old_str 与 new_str 完全相同，未产生任何实际代码变更")
        return self

    def compute_patch_hash(self) -> str:
        """
        计算本次修改动作的 SHA256 唯一摘要
        包含目标文件路径与替换内容，用于状态机检测是否陷入横跳死循环。
        """
        raw_key = f"{self.file_path}:::{self.old_str}:::{self.new_str}"
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]

    def to_unified_diff_preview(self) -> str:
        """
        生成类似 Git 统一差异格式 (Unified Diff) 的补丁预览文本
        便于开发者调试与控制台日志直观展示。
        """
        old_lines = self.old_str.splitlines(keepends=True)
        new_lines = self.new_str.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{self.file_path}",
            tofile=f"b/{self.file_path}",
            lineterm=""
        )
        return "".join(diff)

    @property
    def line_delta(self) -> int:
        """计算本次修改增减的代码行数（正数表示净增行，负数表示净减行）"""
        return len(self.new_str.splitlines()) - len(self.old_str.splitlines())


# =====================================================================
# 3. 单测沙箱执行结果契约
# =====================================================================

class TestResult(BaseModel):
    """
    沙箱内单测执行的标准化返回契约
    封装容器退出码、耗时，以及剪枝提纯后的关键 Traceback 报错帧。
    """
    is_success: bool = Field(..., description="单测是否通过 (exit_code == 0)")
    exit_code: int = Field(..., description="进程退出码")
    raw_logs: str = Field(default="", description="控制台原始输出日志")
    cleaned_traceback: str = Field(default="", description="经过正则清洗的核心报错栈与断言失败信息")
    duration_seconds: float = Field(default=0.0, ge=0.0, description="单测运行耗时（秒）")

    @classmethod
    def from_execution(
        cls,
        exit_code: int,
        raw_logs: str,
        cleaned_traceback: str,
        duration: float
    ) -> "TestResult":
        """工厂方法：从原生命令输出快速构造标准 TestResult 对象"""
        return cls(
            is_success=(exit_code == 0),
            exit_code=exit_code,
            raw_logs=raw_logs,
            cleaned_traceback=cleaned_traceback,
            duration_seconds=round(duration, 3)
        )

    def summary(self) -> str:
        """生成人类友好的测试状态摘要"""
        status = "PASSED (全绿通过)" if self.is_success else f"FAILED (退出码 {self.exit_code})"
        return f"[{status}] 耗时 {self.duration_seconds}s"


# =====================================================================
# 4. 深度反思与规划模型
# =====================================================================

class PatchPlan(BaseModel):
    """
    Plan 节点推理输出的模型修改计划
    指导接下来的代码补丁生成。
    """
    rationale: str = Field(..., description="缺陷产生的根本原因分析")
    target_files: List[str] = Field(..., min_length=1, description="需要修改的目标文件列表")
    planned_edits: List[str] = Field(..., description="具体计划实施的代码变更步骤列表")
    risk_assessment: str = Field(default="低风险", description="本次改动可能带来的潜在风险或副作用")


class ReflexionReport(BaseModel):
    """
    Reflect 节点推理输出的错误深度复盘报告
    提炼失败根因并指导下一轮重试方向。
    """
    failed_hypothesis: str = Field(..., description="上一轮修改所依据但被证明错误的假设")
    root_cause: str = Field(..., description="从单测 Traceback 提取出的真正失败根因")
    next_action_advice: str = Field(..., description="针对下一轮 Patch 修复的具体排查/改动建议")
    should_rollback: bool = Field(default=False, description="本次修改是否破坏了更多语法或用例，需要一键回滚代码")

    def to_prompt_context(self) -> str:
        """格式化为注入下一轮 Prompt 的反思引导文本"""
        rollback_flag = "【警告: 上次改动已引发破坏性错误并被回滚】\n" if self.should_rollback else ""
        return (
            f"{rollback_flag}"
            f"● 失败假设: {self.failed_hypothesis}\n"
            f"● 真正根因: {self.root_cause}\n"
            f"● 下轮行动指南: {self.next_action_advice}"
        )


class TrajectoryStep(BaseModel):
    """智能体执行单步全链路追踪对象，用于 Langfuse / OpenTelemetry 观测日志"""
    step_index: int = Field(..., ge=1, description="当前步骤序号")
    node_name: str = Field(..., description="执行的图节点名称 (如 'localize', 'patch')")
    action_summary: str = Field(..., description="本步骤执行的动作描述")
    observation: str = Field(..., description="动作执行后得到的观察反馈")
    token_usage: Optional[int] = Field(default=None, description="本步消耗的 Token 数量")


# =====================================================================
# 5. LangGraph 全局流转状态与辅助函数 (AgentState)
# =====================================================================

class AgentState(TypedDict):
    """
    驱动 LangGraph 状态图全生命周期流转的核心上下文状态字典
    包含任务输入、AST 定位信息、循环迭代反思以及最终收敛结果。
    """
    # 任务输入
    repo_path: str                          # 目标代码仓库本地路径
    issue_description: str                  # 待修复的 Issue 描述
    test_command: str                       # 验证复现用单测命令

    # 代码探索与定位阶段
    file_tree: List[str]                    # 仓库源码文件路径列表
    suspect_files: List[str]                # 经 AST 定位的高嫌疑文件列表
    relevant_symbols: List[Dict[str, Any]]  # 相关符号代码切片列表
    current_plan: Optional[PatchPlan]       # 当前生成的修复规划

    # 循环修复与反思阶段
    iteration: int                          # 当前迭代轮数（从 1 开始）
    max_iterations: int                     # 最大允许反思迭代轮数
    patch_history_hashes: List[str]         # 历史 Patch Hash，防止死循环横跳
    current_test_result: Optional[TestResult]# 最近一次单测执行结果
    reflections: List[str]                  # 历次单测失败的根因反思记录
    trajectory: List[TrajectoryStep]        # 全流程轨迹步骤追踪

    # 最终收敛状态
    is_resolved: bool                       # 是否最终全绿修复成功
    final_git_diff: Optional[str]           # 成功后导出的完整 Git Diff 补丁


def create_initial_state(
    repo_path: str,
    issue_description: str,
    test_command: str,
    max_iterations: int = 5
) -> AgentState:
    """
    工厂函数：初始化一个干净规范的 AgentState 实例
    :param repo_path: 目标仓库本地根目录
    :param issue_description: 待修复的 Bug/Issue 说明
    :param test_command: 针对该缺陷的复现测试命令 (如 'pytest tests/test_core.py')
    :param max_iterations: 最大允许反思迭代轮次，默认 5 轮
    :return: 初始化的 AgentState 字典
    """
    abs_repo = os.path.abspath(repo_path)
    return AgentState(
        repo_path=abs_repo,
        issue_description=issue_description.strip(),
        test_command=test_command.strip(),
        file_tree=[],
        suspect_files=[],
        relevant_symbols=[],
        current_plan=None,
        iteration=1,
        max_iterations=max_iterations,
        patch_history_hashes=[],
        current_test_result=None,
        reflections=[],
        trajectory=[],
        is_resolved=False,
        final_git_diff=None,
    )


def is_deadlock(state: AgentState, lookback_window: int = 4) -> bool:
    """
    死锁与死循环检测算法：
    检查 patch_history_hashes 中是否存在重复出现的补丁 Hash。
    如果在最近窗口中产生了与历史完全相同的 Patch Hash，判定智能体进入了“改动横跳死锁”。
    :param state: 当前智能体状态
    :param lookback_window: 回溯检测的 Hash 窗口大小
    :return: True 表示检测到死循环死锁，应触发紧急熔断
    """
    hashes = state.get("patch_history_hashes", [])
    if len(hashes) < 2:
        return False

    recent_hashes = hashes[-lookback_window:]
    # 如果最近窗口中存在重复的 hash，说明生成了相同的补丁
    return len(recent_hashes) != len(set(recent_hashes))
