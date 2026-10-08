# -*- coding: utf-8 -*-
"""
GitHealer 顶层主入口与命令行驱动 (Main Entrypoint)
================================================================================
连接 core, tools, workflow 和 eval，提供单任务 CLI 执行与自愈演示模式。

支持功能：
1. 单缺陷自愈修复: python main.py --repo <路径> --issue <问题> --test <单测命令>
2. 一键开箱即用演示: python main.py --demo
"""

import os
import sys
import argparse
import tempfile
import logging

# 确保项目根路径在 sys.path 中
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 确保在 Windows 控制台下支持 UTF-8 输出，防止 Emoji 编码异常
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from src.core.schemas import AgentState, create_initial_state
from src.tools.ast_indexer import RepoASTIndexer
from src.tools.code_editor import CodeEditor
from src.tools.docker_sandbox import DockerSandbox
from src.workflow.nodes import AgentNodes
from src.workflow.graph import GitHealerWorkflow

# 配置终端日志输出格式
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("GitHealer.Main")


def run_single_issue_fix(
    repo_path: str,
    issue_text: str,
    test_cmd: str,
    max_iterations: int = 5
) -> AgentState:
    """
    单任务执行接口：全流程驱动 GitHealer 完成指定 Bug 的自愈修复
    :param repo_path: 目标代码仓库本地路径 (所有操作的根目录基准)
    :param issue_text: Bug/Issue 需求描述
    :param test_cmd: 验证用单测命令 (例如 'pytest tests/test_calc.py')
    :param max_iterations: 最大修复重试次数
    :return: 任务终态 AgentState，包含 Git Diff 与解决标志
    """
    abs_repo = os.path.abspath(repo_path)
    logger.info("=" * 65)
    logger.info("  🚀 GitHealer 自愈式代码智能体启动")
    logger.info("=" * 65)
    logger.info(f"📁 目标仓库根目录 (repo_path): {abs_repo}")
    logger.info(f"📝 待解决 Issue 需求: {issue_text}")
    logger.info(f"🧪 验证用测试指令: {test_cmd}")
    logger.info(f"🔄 最大反思迭代配额: {max_iterations} 轮")
    logger.info("=" * 65)

    # 1. 初始化底座工具集合
    indexer = RepoASTIndexer(repo_path=abs_repo)
    editor = CodeEditor(repo_path=abs_repo)
    sandbox = DockerSandbox(repo_host_path=abs_repo)

    try:
        # 2. 组装智能体算子与 LangGraph 状态图
        nodes = AgentNodes(indexer=indexer, editor=editor, sandbox=sandbox)
        workflow = GitHealerWorkflow(nodes=nodes)
        workflow.build_graph()

        # 3. 初始化并驱动工作流执行
        initial_state = create_initial_state(
            repo_path=abs_repo,
            issue_description=issue_text,
            test_command=test_cmd,
            max_iterations=max_iterations
        )
        final_state = workflow.run(initial_state)

        # 4. 终端可视化展示执行结果
        print("\n" + "=" * 65)
        if final_state.get("is_resolved", False):
            print("  🎉 [任务成功] GitHealer 已完成缺陷自愈修复！单测全绿通过！")
            print("=" * 65)
            print(f"• 累计迭代轮次: {final_state.get('iteration', 1)} 轮")
            print(f"• 执行轨迹步数: {len(final_state.get('trajectory', []))} 步")
            diff = final_state.get("final_git_diff")
            if diff:
                print("\n📋 【生成的 Git 补丁预览 (Unified Diff)】:")
                print("-" * 50)
                print(diff)
                print("-" * 50)
        else:
            print("  ❌ [任务未收敛] GitHealer 在最大重试配额内未完全解决该缺陷。")
            print("=" * 65)
            last_test = final_state.get("current_test_result")
            if last_test:
                print(f"• 最终单测状态: {last_test.summary()}")
                print(f"• 最终错误摘要: {last_test.cleaned_traceback[:300]}")
            if final_state.get("reflections"):
                print("\n🧠 【累计沉淀的反思经验 (Reflections)】:")
                for i, ref in enumerate(final_state["reflections"], 1):
                    print(f"[{i}] {ref}")

        return final_state

    finally:
        # 清理沙箱资源
        sandbox.close()


def run_builtin_demo() -> None:
    """
    一键开箱即用演示：在临时沙箱目录中构造一个包含真实 Bug 的小微仓库，
    驱动 GitHealer 进行端到端自愈修复演示。
    """
    logger.info(">>> 正在准备内置自愈演示环境...")
    with tempfile.TemporaryDirectory() as temp_dir:
        # 1. 构造一个有 Bug 的代码文件（未处理百分比形式如 20.0 的输入）
        buggy_code = '''def calculate_discount(price: float, discount_rate: float) -> float:
    """计算折扣价格（存在未适配百分比输入导致负数的 Bug）"""
    return price * (1.0 - discount_rate)
'''
        calc_path = os.path.join(temp_dir, "calculator.py")
        with open(calc_path, "w", encoding="utf-8") as f:
            f.write(buggy_code)

        # 2. 构造复现测试脚本
        test_script = '''import sys
from calculator import calculate_discount

# 测试用例 1: 正常八折
assert calculate_discount(100.0, 0.2) == 80.0, "Test 1 Failed"

# 测试用例 2: 80% 折扣输入
assert calculate_discount(100.0, 20.0) == 80.0, "Test 2 Failed"

# 测试用例 3: 零折扣
assert calculate_discount(50.0, 0.0) == 50.0, "Test 3 Failed"

print("ALL TESTS PASSED SUCCESSFULLY!")
'''
        test_path = os.path.join(temp_dir, "test_calc.py")
        with open(test_path, "w", encoding="utf-8") as f:
            f.write(test_script)

        # 3. 运行自愈
        issue_desc = "calculator.py 中的 calculate_discount 函数在传入 20.0% 折扣时计算正确，但对于 0.0 折扣缺少完备测试支持。"
        test_cmd = f"{sys.executable} test_calc.py"

        run_single_issue_fix(
            repo_path=temp_dir,
            issue_text=issue_desc,
            test_cmd=test_cmd,
            max_iterations=3
        )


def main() -> None:
    """系统命令行交互主入口"""
    parser = argparse.ArgumentParser(
        description="GitHealer: 基于 AST 剪枝与沙箱测试反馈的自愈式代码智能体"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="运行内置的微型仓库端到端自愈演示"
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=".",
        help="目标代码仓库本地路径 (repo_path)"
    )
    parser.add_argument(
        "--issue",
        type=str,
        default="",
        help="待修复的 Bug 或 Issue 需求描述文本"
    )
    parser.add_argument(
        "--test",
        type=str,
        default="pytest",
        help="用于复现并验证缺陷的测试命令"
    )
    parser.add_argument(
        "--max-iter",
        type=int,
        default=5,
        help="最大允许反思重试的轮次上限"
    )

    args = parser.parse_args()

    if args.demo or not args.issue:
        # 如果未传入具体 Issue 参数或显式指定 --demo，运行演示
        run_builtin_demo()
    else:
        run_single_issue_fix(
            repo_path=args.repo,
            issue_text=args.issue,
            test_cmd=args.test,
            max_iterations=args.max_iter
        )


if __name__ == "__main__":
    main()
