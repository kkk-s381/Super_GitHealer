# -*- coding: utf-8 -*-
"""
LangGraph 环形工作流编排与路由模块 (Graph Workflow)
================================================================================
本模块负责组装驱动 GitHealer 的核心有向循环状态图（Cyclic State Graph）。

核心状态流转拓扑：
  [START]
     │
     ▼
[localize] (定位嫌疑代码)
     │
     ▼
  [plan]   (生成修复规划)
     │
     ▼
 ┌►[patch] (生成并应用代码补丁)
 │   │
 │   ▼
 │ [test]  (沙箱运行单测，捕获 Traceback)
 │   │
 │   ▼
 │ {decide_next_step} ──(单测通过 is_resolved)───────► [END] (成功交付 PR)
 │   │               ──(达到重试上限或死锁熔断)─────► [END] (安全熔断退出)
 │   │
 └──[reflect] (提炼失败根因，更新记忆指南) ◄──(单测失败且在重试配额内)
"""

import logging
from typing import Any, Dict, Optional, Iterator
from langgraph.graph import StateGraph, END, START
from langgraph.graph.state import CompiledStateGraph

from src.core.schemas import AgentState, is_deadlock
from src.workflow.nodes import AgentNodes

logger = logging.getLogger("GitHealer.Workflow")


class GitHealerWorkflow:
    """使用 LangGraph 组装具备回滚反思与死锁检测的自愈闭环状态图"""

    def __init__(self, nodes: AgentNodes) -> None:
        """
        初始化工作流实例
        :param nodes: 包含各阶段推理与执行算子的 AgentNodes 实例
        """
        self.nodes = nodes
        self.compiled_app: Optional[CompiledStateGraph] = None

    def decide_next_step(self, state: AgentState) -> str:
        """
        核心条件路由函数：
        根据当前执行状态动态决定下一跳走向：
        1. 若单测通过 (is_resolved == True) -> 走向 "to_finish" (成功收敛)
        2. 若检测到死循环修改 (hash 碰撞重复) -> 走向 "to_abort" (死锁保护熔断)
        3. 若超过最大迭代上限 (iteration >= max_iterations) -> 走向 "to_abort" (配额耗尽熔断)
        4. 否则 -> 走向 "to_reflect" (进入深度复盘反思，闭环重试)

        :param state: 当前全局上下文状态
        :return: 分支标识 ('to_finish' | 'to_reflect' | 'to_abort')
        """
        # 分支 1：单测全绿通过
        if state.get("is_resolved", False):
            logger.info(">>> 【路由决策】单测全绿通过，任务成功修复！准备导出 PR 补丁。")
            return "to_finish"

        # 分支 2：检测是否陷入改动横跳死锁
        if is_deadlock(state):
            logger.warning(">>> 【路由决策】检测到补丁 Hash 周期性重复碰撞（改动横跳死锁），触发安全熔断！")
            return "to_abort"

        # 分支 3：重试轮次已达上限
        current_iter = state.get("iteration", 1)
        max_iter = state.get("max_iterations", 5)
        if current_iter >= max_iter:
            logger.warning(f">>> 【路由决策】已达到最大重试上限 ({current_iter}/{max_iter})，触发配额熔断。")
            return "to_abort"

        # 分支 4：失败且仍有重试配额，触发自我反思
        logger.info(f">>> 【路由决策】单测失败，进入第 {current_iter} 轮反思复盘闭环...")
        return "to_reflect"

    def build_graph(self) -> CompiledStateGraph:
        """
        构建 LangGraph StateGraph 图拓扑：
        1. 注册核心节点: localize, plan, patch, test, reflect
        2. 设置起始边: START -> localize
        3. 顺序串联边: localize -> plan -> patch -> test
        4. 条件分支边: test -> {to_finish: END, to_abort: END, to_reflect: reflect}
        5. 闭环反馈边: reflect -> patch (实现 Reflexion 自愈循环)
        6. 编译并返回可执行的 CompiledStateGraph 实例

        :return: 编译后的图工作流实例
        """
        builder = StateGraph(AgentState)

        # 1. 注册五大推理与执行节点
        builder.add_node("localize", self.nodes.localize_node)
        builder.add_node("plan", self.nodes.plan_node)
        builder.add_node("patch", self.nodes.patch_node)
        builder.add_node("test", self.nodes.test_node)
        builder.add_node("reflect", self.nodes.reflect_node)

        # 2. 设置入口
        builder.add_edge(START, "localize")

        # 3. 顺序流转连接
        builder.add_edge("localize", "plan")
        builder.add_edge("plan", "patch")
        builder.add_edge("patch", "test")

        # 4. 条件路由分支（核心决策点）
        builder.add_conditional_edges(
            "test",
            self.decide_next_step,
            {
                "to_finish": END,
                "to_abort": END,
                "to_reflect": "reflect"
            }
        )

        # 5. 反思自愈环：反思后重新尝试打补丁
        builder.add_edge("reflect", "patch")

        # 6. 编译图拓扑
        self.compiled_app = builder.compile()
        return self.compiled_app

    def run(self, initial_state: AgentState) -> AgentState:
        """
        同步驱动工作流执行直到收敛终态
        :param initial_state: 初始状态字典
        :return: 最终终态字典
        """
        if self.compiled_app is None:
            self.build_graph()

        logger.info(">>> 启动 GitHealer 智能体状态图执行流...")
        final_state = self.compiled_app.invoke(initial_state)
        return final_state

    def stream(self, initial_state: AgentState) -> Iterator[Dict[str, Any]]:
        """
        流式驱动工作流执行，实时产出每个节点的中间状态变化
        便于连接 CLI 进度条、Langfuse 链路观测或 Web UI
        :param initial_state: 初始状态字典
        :return: 实时产生状态切片的生成器
        """
        if self.compiled_app is None:
            self.build_graph()

        for chunk in self.compiled_app.stream(initial_state):
            yield chunk

    def export_mermaid(self) -> str:
        """
        导出当前状态图的 Mermaid 流程图描述文本，便于生成可视化图表
        :return: Mermaid 格式字符串
        """
        if self.compiled_app is None:
            self.build_graph()
        return self.compiled_app.get_graph().draw_mermaid()
