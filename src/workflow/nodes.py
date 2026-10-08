# -*- coding: utf-8 -*-
"""
状态机各阶段算子实现模块 (Agent Nodes)
================================================================================
本模块实现了驱动 LangGraph 状态图的五大核心认知与执行算子：
1. localize_node: 语法级嫌疑代码定位（结合 Issue 与 AST 大纲）
2. plan_node: 结构化修复规划（生成 PatchPlan）
3. patch_node: 精准代码修改与死锁哈希校验（生成并应用 FileEditAction）
4. test_node: 环境交互与单测验证（调用 DockerSandbox）
5. reflect_node: 报错根因反思与故障回滚决策（生成 ReflexionReport，触发 Rollback）
"""

import os
import re
import json
import logging
import time
from typing import Dict, Any, List, Optional

from src.core.schemas import (
    AgentState,
    FileEditAction,
    PatchPlan,
    ReflexionReport,
    TrajectoryStep,
    TestResult,
)
from src.core.config import AgentConfig
from src.tools.ast_indexer import RepoASTIndexer
from src.tools.code_editor import CodeEditor
from src.tools.docker_sandbox import DockerSandbox

try:
    import openai
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

logger = logging.getLogger("GitHealer.Nodes")


# =====================================================================
# 核心 Prompt 模板库 (Prompt Engineering)
# =====================================================================

SYSTEM_LOCALIZE_PROMPT = """你是一个专业的代码缺陷定位专家（SWE Fault Localizer）。
你的任务是根据用户反馈的 Issue 问题描述和代码仓库的文件树大纲，推断出最可能存在 Bug 的嫌疑文件与函数。

输出要求：
必须输出合法的 JSON 格式，包含字段：
{
  "suspect_files": ["最相关的源码文件相对路径列表"],
  "suspect_symbols": ["最相关的函数或类名列表"],
  "reasoning": "为什么锁定这些文件的推导依据"
}
"""

SYSTEM_PLAN_PROMPT = """你是一个顶级软件架构师与调试专家。
针对当前 Issue 缺陷、嫌疑代码以及历史上的失败反思记录，制定一份清晰、精确的代码修复策略（Patch Plan）。

输出要求：
必须输出合法的 JSON 格式，匹配 PatchPlan 结构：
{
  "rationale": "缺陷产生的根因与设计缺陷分析",
  "target_files": ["需要实施修改的目标文件相对路径列表"],
  "planned_edits": ["分步实施的具体修改动作清单"],
  "risk_assessment": "该修改可能对其他模块产生的潜在回归风险"
}
"""

SYSTEM_PATCH_PROMPT = """你是一个精准的代码补丁生成器（Code Patch Generator）。
你必须输出且仅输出一个基于 Search-and-Replace（块级替换）的代码修改动作。

【至关重要的规则】：
1. old_str 必须与目标文件中的现有代码【100% 精确匹配】，包括所有空格、换行和缩进，禁止概括或省略！
2. new_str 是你用于替换的新代码，必须保持正确的缩进与语法。
3. 如果有历史失败反思（Reflections），必须吸取教训，绝对不要重复之前被证明错误的修改路径！

输出要求：
必须输出合法的 JSON 格式，匹配 FileEditAction 结构：
{
  "file_path": "目标文件相对路径",
  "old_str": "原文件中需要被替换的原样代码块",
  "new_str": "替换后的全新代码块",
  "explanation": "本次修改的逻辑说明"
}
"""

SYSTEM_REFLECT_PROMPT = """你是一个代码调试复盘专家（Reflexion Critic）。
刚才智能体实施的代码修改未通过单元测试，控制台捕获到了错误堆栈（Traceback）。
请深度剖析失败原因，指导下一轮修复。

输出要求：
必须输出合法的 JSON 格式，匹配 ReflexionReport 结构：
{
  "failed_hypothesis": "上一轮修改所依据但被证明错误的假设",
  "root_cause": "从 Traceback 报错帧中提取的真实失败根因",
  "next_action_advice": "对下一轮代码修改的具体排查与换向建议",
  "should_rollback": true/false (若本次修改导致严重语法崩溃或更多测试爆错，设为 true)
}
"""


# =====================================================================
# 智能体各节点算子集合 (AgentNodes)
# =====================================================================

class AgentNodes:
    """定义驱动 LangGraph 状态机中各个节点（Node）的具体推理与执行算子"""

    def __init__(
        self,
        indexer: RepoASTIndexer,
        editor: CodeEditor,
        sandbox: DockerSandbox,
        llm_client: Any = None,
        model_name: Optional[str] = None
    ) -> None:
        """
        初始化智能体算子集合
        :param indexer: AST 语法树检索器实例
        :param editor: 精准补丁编辑器实例
        :param sandbox: Docker 沙箱测试运行器实例
        :param llm_client: 大语言模型调用客户端（支持 OpenAI / DeepSeek 等兼容客户端）
        :param model_name: 调用的模型名称，默认读取 AgentConfig.DEFAULT_MODEL
        """
        self.indexer = indexer
        self.editor = editor
        self.sandbox = sandbox
        self.model_name = model_name or AgentConfig.DEFAULT_MODEL
        self.llm_client = llm_client

        # 尝试自动从环境变量探测 OpenAI 兼容客户端
        if self.llm_client is None and HAS_OPENAI:
            api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
            base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("DEEPSEEK_BASE_URL")
            if api_key:
                try:
                    self.llm_client = openai.OpenAI(api_key=api_key, base_url=base_url)
                except Exception:
                    self.llm_client = None

        # 记录每轮修改前创建的快照 ID，供 reflect 节点可能的回滚使用
        self._last_checkpoint_id: Optional[str] = None
        self._last_suspect_files: List[str] = []

    def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        """
        调用大语言模型生成结构化文本
        若未配置 API Key 或请求异常，自动使用内置的确定性启发式推理器（Heuristic Reasoner）
        确保系统即使在无 Key 离线状态下也能进行流程走通演示。
        """
        if self.llm_client is not None:
            try:
                response = self.llm_client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=AgentConfig.TEMPERATURE,
                )
                return response.choices[0].message.content or ""
            except Exception as e:
                err_msg = f"大模型接口调用失败 ({e})！请检查模型名称是否匹配该服务商（例如 DeepSeek 平台不支持 gemini 模型）或 Base URL 是否有效。"
                logger.error(f"❌ {err_msg}")
                raise RuntimeError(err_msg)

        # 仅在未配置任何大模型（完全离线）时，提示并使用兜底规则
        logger.info("ℹ️ 未配置大模型 API，使用内置规则离线演示")
        return self._heuristic_fallback(system_prompt, user_prompt)

    def _extract_json(self, raw_text: str) -> Dict[str, Any]:
        """从模型回复中提取并反序列化 JSON，兼容 markdown 代码块包裹"""
        # 匹配 ```json ... ``` 或直接的 { ... }
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw_text)
        cleaned = match.group(1).strip() if match else raw_text.strip()
        try:
            return json.loads(cleaned)
        except Exception:
            # 尝试截取最外层的 { ... }
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1:
                return json.loads(cleaned[start:end+1])
            raise ValueError(f"无法从模型输出中提取有效 JSON: {raw_text[:200]}")

    def _heuristic_fallback(self, system_prompt: str, user_prompt: str) -> str:
        """启发式规则兜底引擎（离线与测试模式）"""
        if "SWE Fault Localizer" in system_prompt:
            files = [f for f in self.indexer.get_file_tree() if not os.path.basename(f).startswith("test")]
            if not files:
                files = self.indexer.get_file_tree()
            target = files[0] if files else "main.py"
            self._last_suspect_files = [target]
            return json.dumps({
                "suspect_files": [target],
                "suspect_symbols": [],
                "reasoning": f"根据仓库源码结构优先锁定待测业务文件: {target}"
            })
        elif "Patch Plan" in system_prompt:
            target = self._last_suspect_files[0] if self._last_suspect_files else "main.py"
            return json.dumps({
                "rationale": "基于单测报错分析出参数边界判断与会员折算逻辑存在偏差。",
                "target_files": [target],
                "planned_edits": ["修复满减临界门槛判断", "调整 VIP 折扣系数", "补充下限保底"],
                "risk_assessment": "低风险，局部单函数修补"
            })
        elif "Code Patch Generator" in system_prompt:
            target = self._last_suspect_files[0] if self._last_suspect_files else "main.py"
            content = self.editor.read_file(target)
            if "calculate_order_total" in content:
                old_chunk = """            if discounted_amount > threshold:
                discounted_amount -= amount
        elif coupon_type == "PERCENT":
            rate = float(coupon.get("rate", 1.0))
            discounted_amount *= rate

    # -----------------------------------------------------------------
    # 🐛 BUG 2 (会员折算逻辑缺陷):
    # VIP 专享 9 折，错误地直接乘以了 0.1，导致实付金额变成了原价的 10%
    # 正确逻辑应为: discounted_amount *= 0.9
    # -----------------------------------------------------------------
    if vip_level == "VIP":
        discounted_amount = discounted_amount * 0.1
    elif vip_level == "SVIP":
        discounted_amount = discounted_amount * 0.8

    # -----------------------------------------------------------------
    # 🐛 BUG 3 (缺少下限防御兜底):
    # 当优惠券抵扣金额超过订单原价时，未做 0 元保底，导致实付金额出现负数
    # 正确逻辑应为: discounted_amount = max(0.0, discounted_amount)
    # -----------------------------------------------------------------
    return round(discounted_amount, 2)"""
                new_chunk = """            if discounted_amount >= threshold:
                discounted_amount -= amount
        elif coupon_type == "PERCENT":
            rate = float(coupon.get("rate", 1.0))
            discounted_amount *= rate

    # 修复会员权益: VIP 享 9 折 (0.9)
    if vip_level == "VIP":
        discounted_amount = discounted_amount * 0.9
    elif vip_level == "SVIP":
        discounted_amount = discounted_amount * 0.8

    # 修复保底保护: 实付金额不能低于 0.0 元
    discounted_amount = max(0.0, discounted_amount)
    return round(discounted_amount, 2)"""
                return json.dumps({
                    "file_path": target,
                    "old_str": old_chunk,
                    "new_str": new_chunk,
                    "explanation": "修复满减门槛临界值判断、VIP 9折计算与实付下限保底。"
                })
            elif "calculate_discount" in content:
                return json.dumps({
                    "file_path": target,
                    "old_str": "    return price * (1.0 - discount_rate)",
                    "new_str": "    if discount_rate <= 0.0:\n        return price\n    return price * (1.0 - discount_rate)",
                    "explanation": "添加针对零折扣的边界保护。"
                })
            else:
                return json.dumps({
                    "file_path": target,
                    "old_str": "# TODO",
                    "new_str": "# Fixed",
                    "explanation": "基础修改。"
                })
        elif "Reflexion Critic" in system_prompt:
            return json.dumps({
                "failed_hypothesis": "假设直接修改参数类型即可通过测试",
                "root_cause": "单测抛出 AssertionError，期望返回值与实际不符",
                "next_action_advice": "调整逻辑判断分支，适配边界测试用例",
                "should_rollback": False
            })
        return "{}"

    # =================================================================
    # 节点 1：定位嫌疑代码 (Localize)
    # =================================================================
    def localize_node(self, state: AgentState) -> Dict[str, Any]:
        """结合 Issue 描述与项目大纲树，推理出需要重点排查的高嫌疑文件与具体函数"""
        logger.info(">>> [Node: Localize] 正在启动 AST 语法大纲探索与代码定位...")

        file_tree = self.indexer.get_file_tree()
        issue = state["issue_description"]

        # 构建高密度代码大纲摘要（取前 15 个关键文件大纲）
        outline_snippets = []
        for fpath in file_tree[:15]:
            outlines = self.indexer.parse_file_outline(fpath)
            if outlines:
                compact_str = ", ".join([o.name for o in outlines[:4]])
                outline_snippets.append(f"- {fpath}: [{compact_str}]")

        outlines_str = "\n".join(outline_snippets)
        user_prompt = f"""
【待修复的 Issue 需求描述】:
{issue}

【代码仓库可用文件大纲】:
{outlines_str}

请分析并输出最可能需要修改的 suspect_files 和 suspect_symbols。
"""
        raw_res = self._call_llm(SYSTEM_LOCALIZE_PROMPT, user_prompt)
        parsed = self._extract_json(raw_res)

        suspect_files = parsed.get("suspect_files", [])
        if not suspect_files and file_tree:
            suspect_files = [file_tree[0]]

        # 按需展开嫌疑符号的源码切片
        relevant_symbols = []
        for s_file in suspect_files:
            outlines = self.indexer.parse_file_outline(s_file)
            for o in outlines[:3]:
                try:
                    src = self.indexer.get_symbol_source(s_file, o.name)
                    relevant_symbols.append({
                        "file_path": s_file,
                        "name": o.name,
                        "kind": o.kind,
                        "code": src
                    })
                except Exception:
                    continue

        step = TrajectoryStep(
            step_index=len(state.get("trajectory", [])) + 1,
            node_name="localize",
            action_summary=f"定位出 {len(suspect_files)} 个嫌疑文件",
            observation=f"锁定文件: {suspect_files}",
        )

        return {
            "file_tree": file_tree,
            "suspect_files": suspect_files,
            "relevant_symbols": relevant_symbols,
            "trajectory": state.get("trajectory", []) + [step]
        }

    # =================================================================
    # 节点 2：生成修复策略 (Plan)
    # =================================================================
    def plan_node(self, state: AgentState) -> Dict[str, Any]:
        """对比当前代码逻辑与预期行为，形成修改分步规划"""
        logger.info(">>> [Node: Plan] 正在推演修复策略并制定实施规划...")

        issue = state["issue_description"]
        suspects = state["suspect_files"]
        symbols = state.get("relevant_symbols", [])
        reflections = state.get("reflections", [])
        symbol_snippets = []
        for s in symbols[:3]:
            code_preview = s['code'][:400]
            symbol_snippets.append("--- " + s['file_path'] + " :: " + s['name'] + " ---\n" + code_preview)
        symbols_ctx = "\n\n".join(symbol_snippets)
        reflections_ctx = "\n".join(reflections[-2:]) if reflections else "暂无历史反思（首轮尝试）"

        user_prompt = f"""
【Issue 描述】: {issue}
【目标嫌疑文件】: {suspects}
【关键源码上下文】:
{symbols_ctx}

【过往失败教训 (Reflections)】:
{reflections_ctx}

请输出完整的修复规划 JSON (PatchPlan)。
"""
        raw_res = self._call_llm(SYSTEM_PLAN_PROMPT, user_prompt)
        parsed = self._extract_json(raw_res)

        plan = PatchPlan(
            rationale=parsed.get("rationale", "定位逻辑错误并实施规避。"),
            target_files=parsed.get("target_files", suspects),
            planned_edits=parsed.get("planned_edits", ["修改目标函数边界条件"]),
            risk_assessment=parsed.get("risk_assessment", "低风险")
        )

        step = TrajectoryStep(
            step_index=len(state.get("trajectory", [])) + 1,
            node_name="plan",
            action_summary="制定修复计划 PatchPlan",
            observation=f"计划目标: {plan.planned_edits}",
        )

        return {
            "current_plan": plan,
            "trajectory": state.get("trajectory", []) + [step]
        }

    # =================================================================
    # 节点 3：代码修改与执行 (Patch)
    # =================================================================
    def patch_node(self, state: AgentState) -> Dict[str, Any]:
        """结合上下文与历史反思，生成精确的 FileEditAction 并通过 CodeEditor 应用"""
        logger.info(f">>> [Node: Patch] 正在生成并应用代码补丁 (第 {state.get('iteration', 1)} 轮)...")

        # 1. 动手术前，创建一键回滚快照点
        self._last_checkpoint_id = self.editor.create_checkpoint()

        plan = state.get("current_plan")
        suspects = state.get("suspect_files", [])
        target_file = suspects[0] if suspects else "calculator.py"

        # 实时读取目标文件在磁盘上的最新真实内容，防止 LLM 脑补变量名
        live_content = self.editor.read_file(target_file)
        if live_content:
            source_ctx = f"【待修改目标文件 ({target_file}) 当前磁盘真实源码】:\n```python\n{live_content}\n```"
        else:
            symbols = state.get("relevant_symbols", [])
            patch_symbol_snippets = ["【" + s['file_path'] + "】\n" + s['code'] for s in symbols[:2]]
            source_ctx = "\n\n".join(patch_symbol_snippets)

        reflections = state.get("reflections", [])
        reflections_ctx = "\n".join(reflections[-2:]) if reflections else "首轮修复"

        last_patch_err = state.get("last_patch_error")
        if not state.get("apply_success", True) and last_patch_err:
            patch_warning = (
                f"\n⚠️【上轮补丁未生效严重警告】:\n"
                f"上一轮你提交的补丁写入失败，原因: {last_patch_err}\n"
                f"请注意：由于 old_str 未能精确匹配，代码并未修改，单测依然是在测旧代码！\n"
                f"【必须遵守的要求】: 请务必直接从上方【待修改目标文件当前磁盘真实源码】中完整复制 old_str，绝对禁止自己杜撰任何变量名或缩进！\n"
            )
        else:
            patch_warning = ""

        user_prompt = f"""
{source_ctx}

【修复规划】: {plan.planned_edits if plan else '修复缺陷'}

{patch_warning}【历史失败警示】:
{reflections_ctx}

请输出一个精准的 Search-and-Replace 补丁 JSON (FileEditAction)。
【核心规则】：
1. old_str 必须直接从上方的真实源码中原样复制，100% 精确匹配（包括空格、换行和变量名）！
2. new_str 是替换后的新代码。
"""
        raw_res = self._call_llm(SYSTEM_PATCH_PROMPT, user_prompt)
        parsed = self._extract_json(raw_res)

        action = None
        apply_success = False
        patch_hash = "invalid_patch"
        obs = ""

        try:
            action = FileEditAction(
                file_path=parsed.get("file_path", target_file),
                old_str=parsed.get("old_str", ""),
                new_str=parsed.get("new_str", ""),
                explanation=parsed.get("explanation", "自动修复补丁")
            )
            patch_hash = action.compute_patch_hash()
            apply_success = self.editor.apply_replace_patch(action)
            obs = f"补丁应用成功: {action.file_path} (Delta: {action.line_delta} 行)"
        except Exception as e:
            obs = f"补丁定义无效或应用失败: {str(e)}"
            logger.warning(obs)
            apply_success = False

        history_hashes = list(state.get("patch_history_hashes", [])) + [patch_hash]

        current_iter = state.get("iteration", 1)
        step = TrajectoryStep(
            step_index=len(state.get("trajectory", [])) + 1,
            node_name="patch",
            action_summary=f"应用修改补丁 Hash[{patch_hash}] (成功: {apply_success})",
            observation=obs,
        )

        return {
            "patch_history_hashes": history_hashes,
            "iteration": current_iter,
            "trajectory": state.get("trajectory", []) + [step],
            "apply_success": apply_success,
            "last_patch_error": None if apply_success else obs
        }

    # =================================================================
    # 节点 4：沙箱验证 (Test)
    # =================================================================
    def test_node(self, state: AgentState) -> Dict[str, Any]:
        """调用 DockerSandbox 运行复现单测，清洗并捕获 Traceback 结果"""
        logger.info(">>> [Node: Test] 正在沙箱中执行单测验证...")

        test_cmd = state["test_command"]
        test_result = self.sandbox.run_test_command(test_cmd)

        is_resolved = test_result.is_success
        final_diff = None

        if is_resolved:
            logger.info(">>> 🎉 [Node: Test] 单测通过！准备计算全量 Git Diff...")
            final_diff = self.editor.get_unified_diff()
            obs = f"测试通过！{test_result.summary()}"
        else:
            logger.warning(f">>> ❌ [Node: Test] 单测未通过: {test_result.summary()}")
            obs = f"测试失败，报错: {test_result.cleaned_traceback[:120]}..."

        step = TrajectoryStep(
            step_index=len(state.get("trajectory", [])) + 1,
            node_name="test",
            action_summary=f"执行单测: {test_cmd}",
            observation=obs,
        )

        return {
            "current_test_result": test_result,
            "is_resolved": is_resolved,
            "final_git_diff": final_diff,
            "trajectory": state.get("trajectory", []) + [step]
        }

    # =================================================================
    # 节点 5：错误深度反思 (Reflect)
    # =================================================================
    def reflect_node(self, state: AgentState) -> Dict[str, Any]:
        """单测失败时，提取 Traceback 根因，生成下一轮反思指南并实施故障回滚"""
        logger.info(">>> [Node: Reflect] 正在从单测错误日志中提炼失败根因 (Reflexion)...")

        test_res = state.get("current_test_result")
        traceback_logs = test_res.cleaned_traceback if test_res else "单测未通过，无报错日志"

        last_patch_err = state.get("last_patch_error")
        patch_warning = ""
        if not state.get("apply_success", True) and last_patch_err:
            patch_warning = f"\n【⚠️ 必须引起重视的关键事实】:\n上一轮提交的代码补丁未能成功写入文件 (报错: {last_patch_err})！\n这说明代码并未发生任何修改，当前单测失败依然是未修改时的旧代码导致的。\n在反思中，你的 next_action_advice 必须明确要求：严禁脑补不存在的变量名，必须 100% 对齐源码实际内容进行替换！\n"

        user_prompt = f"""
【执行的单测指令】: {state['test_command']}
【清洗后的错误堆栈 (Traceback)】:
{traceback_logs}
{patch_warning}
请输出深度反思报告 JSON (ReflexionReport)。
"""
        raw_res = self._call_llm(SYSTEM_REFLECT_PROMPT, user_prompt)
        parsed = self._extract_json(raw_res)

        report = ReflexionReport(
            failed_hypothesis=parsed.get("failed_hypothesis", "前序修改假设未能完全覆盖缺陷边界"),
            root_cause=parsed.get("root_cause", "测试断言失败，期望输出与实际逻辑不匹配"),
            next_action_advice=parsed.get("next_action_advice", "检查边缘测试分支，重新对齐返回值类型"),
            should_rollback=parsed.get("should_rollback", False)
        )

        # 若本次修改被判定为破坏性修改，立即触发物理代码回滚
        rollback_msg = ""
        if report.should_rollback and self._last_checkpoint_id:
            logger.warning(">>> ⚠️ [Node: Reflect] 触发自动回滚，恢复至本次修改前的完好状态！")
            self.editor.rollback_to_checkpoint(self._last_checkpoint_id)
            rollback_msg = "（已自动回滚破坏性代码）"

        step = TrajectoryStep(
            step_index=len(state.get("trajectory", [])) + 1,
            node_name="reflect",
            action_summary="生成反思复盘报告",
            observation=f"根因: {report.root_cause} {rollback_msg}",
        )

        new_reflections = list(state.get("reflections", [])) + [report.to_prompt_context()]

        return {
            "iteration": state.get("iteration", 1) + 1,
            "reflections": new_reflections,
            "trajectory": state.get("trajectory", []) + [step]
        }
