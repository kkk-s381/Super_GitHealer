# -*- coding: utf-8 -*-
"""
GitHealer 现代化 Web 可视化控制台 (Web Dashboard)
================================================================================
提供极客风格的 Web 交互界面：
1. 参数配置区：仓库路径、Issue 问题描述、验证单测命令、LLM API Key / 基础模型；
2. 一键内置 Demo 预填与快速启动；
3. 状态机节点动态高亮流转（Localize -> Plan -> Patch -> Test -> Reflect）；
4. 实时控制台日志输出；
5. Git Unified Diff 差异补丁代码高亮与反思历史展示。

基于 Python 标准库 http.server 与多线程驱动，零额外第三方 Web 框架依赖，开箱即用。
"""

import os
import sys
import json
import logging
import threading
import tempfile
import time
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, List, Optional

# 确保在 Windows GBK 控制台下支持 UTF-8 输出，防止 Emoji 编码异常
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 确保项目根路径进入 sys.path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.schemas import AgentState, create_initial_state
from src.tools.ast_indexer import RepoASTIndexer
from src.tools.code_editor import CodeEditor
from src.tools.docker_sandbox import DockerSandbox
from src.workflow.nodes import AgentNodes
from src.workflow.graph import GitHealerWorkflow

try:
    import openai
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

logger = logging.getLogger("GitHealer.WebUI")

# 全局运行态管理器
class TaskManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.is_running = False
        self.status = "idle"  # idle, running, success, failed
        self.current_step = "idle"  # localize, plan, patch, test, reflect, done
        self.iteration = 0
        self.max_iterations = 5
        self.logs: List[str] = []
        self.git_diff = ""
        self.reflections: List[str] = []
        self.summary_msg = ""
        self.should_stop = False
        self.active_sandbox = None

    def reset(self, max_iter: int = 5):
        with self.lock:
            self.is_running = True
            self.status = "running"
            self.current_step = "localize"
            self.iteration = 1
            self.max_iterations = max_iter
            self.logs = []
            self.git_diff = ""
            self.reflections = []
            self.summary_msg = ""
            self.should_stop = False

    def add_log(self, text: str):
        with self.lock:
            self.logs.append(text)

    def update_step(self, step: str, iteration: Optional[int] = None):
        with self.lock:
            self.current_step = step
            if iteration is not None:
                self.iteration = iteration

    def finish(self, success: bool, summary: str, diff: str = "", reflections: Optional[List[str]] = None):
        with self.lock:
            self.is_running = False
            self.status = "success" if success else "failed"
            self.current_step = "done"
            self.summary_msg = summary
            self.git_diff = diff or ""
            if reflections:
                self.reflections = reflections

    def get_snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "is_running": self.is_running,
                "status": self.status,
                "current_step": self.current_step,
                "iteration": self.iteration,
                "max_iterations": self.max_iterations,
                "logs": self.logs[-100:],  # 最多返回最近100条日志
                "git_diff": self.git_diff,
                "reflections": self.reflections,
                "summary": self.summary_msg
            }

task_mgr = TaskManager()


class WebLogHandler(logging.Handler):
    """自定义日志处理器，将工作流日志实时注入 TaskManager"""
    def emit(self, record):
        try:
            msg = self.format(record)
            task_mgr.add_log(msg)
        except Exception:
            pass

# 挂载 WebLogHandler
web_handler = WebLogHandler()
web_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
logging.getLogger("GitHealer").addHandler(web_handler)
logging.getLogger("GitHealer").setLevel(logging.INFO)


def execute_healing_task(
    repo_path: str,
    issue_text: str,
    test_cmd: str,
    max_iterations: int = 5,
    api_key: str = "",
    base_url: str = "",
    model_name: str = "gpt-4o"
):
    """后台工作线程执行自愈流程"""
    abs_repo = os.path.abspath(repo_path)
    task_mgr.add_log(f"🚀 [任务初始化] 启动 GitHealer 代码自愈引擎...")
    task_mgr.add_log(f"📁 目标仓库: {abs_repo}")
    task_mgr.add_log(f"📝 待解决 Issue: {issue_text}")
    task_mgr.add_log(f"🧪 验证命令: {test_cmd}")
    task_mgr.add_log(f"🔄 最大轮次: {max_iterations}")

    # 配置可选的自定义 LLM Client
    llm_client = None
    if api_key and HAS_OPENAI:
        try:
            kwargs = {"api_key": api_key.strip()}
            if base_url and base_url.strip():
                kwargs["base_url"] = base_url.strip()
            llm_client = openai.OpenAI(**kwargs)
            task_mgr.add_log(f"🔑 已装载自定义 LLM 客户端 (模型: {model_name})")
        except Exception as e:
            task_mgr.add_log(f"⚠️ 自定义 LLM 初始化失败，将启用启发式兜底: {e}")

    indexer = RepoASTIndexer(repo_path=abs_repo)
    editor = CodeEditor(repo_path=abs_repo)
    sandbox = DockerSandbox(repo_host_path=abs_repo)
    task_mgr.active_sandbox = sandbox

    try:
        nodes = AgentNodes(
            indexer=indexer,
            editor=editor,
            sandbox=sandbox,
            llm_client=llm_client,
            model_name=model_name
        )
        workflow = GitHealerWorkflow(nodes=nodes)
        workflow.build_graph()

        initial_state = create_initial_state(
            repo_path=abs_repo,
            issue_description=issue_text,
            test_command=test_cmd,
            max_iterations=max_iterations
        )

        task_mgr.add_log(">>> 启动 LangGraph 状态图流式执行...")

        current_iter = 1
        final_state = dict(initial_state)

        # 流式迭代每一个 Node
        for chunk in workflow.stream(initial_state):
            if task_mgr.should_stop:
                task_mgr.add_log("⏹️ 用户手动终止任务执行。")
                task_mgr.finish(False, "任务被用户手动终止")
                return

            for node_name, node_update in chunk.items():
                task_mgr.add_log(f"📍 进入状态机节点: [{node_name.upper()}]")
                final_state.update(node_update)

                iter_num = final_state.get("iteration", current_iter)
                task_mgr.update_step(node_name, iter_num)

                if node_name == "localize":
                    suspects = final_state.get("suspect_files", [])
                    task_mgr.add_log(f"🔍 [Localize] 锁定嫌疑文件: {', '.join(suspects) if suspects else '无'}")
                elif node_name == "plan":
                    task_mgr.add_log("📐 [Plan] 修复方案已就绪，准备应用块级补丁...")
                elif node_name == "patch":
                    if final_state.get("apply_success", True):
                        task_mgr.add_log(f"🛠️ [Patch] 补丁已成功写入工作区 (轮次: {iter_num})")
                    else:
                        err_snip = str(final_state.get("last_patch_error", "代码匹配失败"))[:80]
                        task_mgr.add_log(f"⚠️ [Patch] 补丁未能写入工作区 (old_str 匹配失败, 轮次: {iter_num})")
                elif node_name == "test":
                    test_res = final_state.get("current_test_result")
                    if test_res:
                        if test_res.is_success:
                            task_mgr.add_log("✅ [Test] 沙箱单测全绿通过！准备导出 PR 补丁。")
                        else:
                            task_mgr.add_log(f"❌ [Test] 单测未通过 (退出码 {test_res.exit_code})，进入反思...")
                elif node_name == "reflect":
                    refs = final_state.get("reflections", [])
                    if refs:
                        task_mgr.add_log(f"🧠 [Reflect] 总结经验: {refs[-1][:80]}...")

        # 检查最终状态
        is_resolved = final_state.get("is_resolved", False)
        diff = final_state.get("final_git_diff", "")
        reflections = final_state.get("reflections", [])

        if is_resolved:
            msg = f"🎉 缺陷修复成功！累计迭代 {final_state.get('iteration', 1)} 轮，执行轨迹 {len(final_state.get('trajectory', []))} 步。"
            task_mgr.add_log(msg)
            task_mgr.finish(True, msg, diff, reflections)
        else:
            msg = f"❌ 任务未能在 {max_iterations} 轮配额内收敛。"
            task_mgr.add_log(msg)
            task_mgr.finish(False, msg, diff, reflections)

    except Exception as e:
        err_msg = f"💥 执行发生未捕获异常: {e}\n{traceback.format_exc()}"
        task_mgr.add_log(err_msg)
        task_mgr.finish(False, f"异常报错: {str(e)}")
    finally:
        sandbox.close()
        task_mgr.active_sandbox = None


HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>GitHealer - 自愈式代码智能体可视化工作台</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    @keyframes pulse-slow {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.8; transform: scale(1.03); }
    }
    .step-active {
      animation: pulse-slow 1.5s infinite;
      box-shadow: 0 0 15px rgba(59, 130, 246, 0.6);
    }
    .custom-scroll::-webkit-scrollbar {
      width: 6px;
      height: 6px;
    }
    .custom-scroll::-webkit-scrollbar-track {
      background: #1e293b;
    }
    .custom-scroll::-webkit-scrollbar-thumb {
      background: #475569;
      border-radius: 3px;
    }
  </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen font-sans">
  <!-- 顶部导航 -->
  <header class="border-b border-slate-800 bg-slate-900/80 backdrop-blur sticky top-0 z-50 px-6 py-3.5 flex items-center justify-between">
    <div class="flex items-center space-x-3">
      <div class="w-9 h-9 rounded-lg bg-gradient-to-tr from-cyan-500 to-blue-600 flex items-center justify-center font-bold text-white shadow-lg shadow-cyan-500/20 text-lg">
        ⚡
      </div>
      <div>
        <h1 class="text-lg font-bold tracking-tight text-white flex items-center gap-2">
          GitHealer <span class="text-xs px-2 py-0.5 rounded bg-blue-500/20 text-blue-400 border border-blue-500/30 font-mono">Agent v1.0</span>
        </h1>
        <p class="text-xs text-slate-400">基于 AST 剪枝与沙箱测试反馈的自愈式代码智能体</p>
      </div>
    </div>
    <div class="flex items-center space-x-2">
      <button onclick="fillBusinessDemo()" class="px-3 py-1.5 rounded-lg text-xs font-medium bg-blue-900/50 hover:bg-blue-800/80 text-blue-200 border border-blue-600/40 transition flex items-center gap-1.5">
        <span>🛒</span> 预填电商业务 Bug Demo
      </button>
      <button onclick="fillDemoParams()" class="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition flex items-center gap-1.5">
        <span>🪄</span> 预填沙箱 Demo
      </button>
      <div id="statusBadge" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-slate-800 text-slate-400 border border-slate-700 flex items-center gap-1.5">
        <span class="w-2 h-2 rounded-full bg-slate-500"></span> 空闲就绪
      </div>
    </div>
  </header>

  <!-- 主容器 -->
  <main class="max-w-7xl mx-auto px-6 py-6 grid grid-cols-1 lg:grid-cols-12 gap-6">
    
    <!-- 左侧：输入与控制面板 (5列) -->
    <section class="lg:col-span-5 space-y-5">
      <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-xl">
        <h2 class="text-sm font-semibold text-slate-200 uppercase tracking-wider mb-4 flex items-center gap-2">
          <span class="text-blue-400">⚙️</span> 任务输入与环境配置
        </h2>

        <form id="agentForm" onsubmit="handleFormSubmit(event)" class="space-y-4">
          <!-- 目标仓库目录 -->
          <div>
            <label class="block text-xs font-medium text-slate-300 mb-1">目标代码仓库路径 (repo_path)</label>
            <input type="text" id="repoPath" required placeholder="例如: D:/my_repo 或绝对路径"
              class="w-full px-3 py-2 rounded-lg bg-slate-950 border border-slate-800 text-xs text-slate-200 placeholder-slate-600 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 font-mono">
            <p class="text-[11px] text-slate-500 mt-1">智能体在此目录执行 AST 索引、代码查找与补丁写入</p>
          </div>

          <!-- Issue 描述 -->
          <div>
            <label class="block text-xs font-medium text-slate-300 mb-1">缺陷 / Issue 需求描述</label>
            <textarea id="issueText" rows="3" required placeholder="请清晰描述 Bug 表现、期望逻辑或复现条件..."
              class="w-full px-3 py-2 rounded-lg bg-slate-950 border border-slate-800 text-xs text-slate-200 placeholder-slate-600 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 font-mono"></textarea>
          </div>

          <!-- 测试命令 -->
          <div>
            <label class="block text-xs font-medium text-slate-300 mb-1">复现与验证测试命令 (test_cmd)</label>
            <input type="text" id="testCmd" required placeholder="例如: pytest tests/test_calc.py 或 python test.py"
              class="w-full px-3 py-2 rounded-lg bg-slate-950 border border-slate-800 text-xs text-slate-200 placeholder-slate-600 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 font-mono">
          </div>

          <!-- 高级配置折叠 -->
          <details class="bg-slate-950/60 border border-slate-800/80 rounded-lg p-3 text-xs">
            <summary class="font-medium text-slate-300 cursor-pointer select-none flex items-center justify-between">
              <span>🔧 大模型推理配置 (可选，留空则使用内置规则兜底)</span>
              <span class="text-slate-500">▼</span>
            </summary>
            <div class="mt-3 space-y-3 pt-2 border-t border-slate-800">
              <div>
                <label class="block text-[11px] text-slate-400 mb-1">API Key</label>
                <input type="password" id="apiKey" placeholder="sk-..."
                  class="w-full px-2.5 py-1.5 rounded bg-slate-900 border border-slate-800 text-xs text-slate-200 font-mono focus:outline-none focus:border-blue-500">
              </div>
              <div class="grid grid-cols-2 gap-2">
                <div>
                  <label class="block text-[11px] text-slate-400 mb-1">Base URL (可选代理/中转)</label>
                  <input type="text" id="baseUrl" placeholder="https://api.openai.com/v1"
                    class="w-full px-2.5 py-1.5 rounded bg-slate-900 border border-slate-800 text-xs text-slate-200 font-mono focus:outline-none focus:border-blue-500">
                </div>
                <div>
                  <label class="block text-[11px] text-slate-400 mb-1">模型名称 (Model)</label>
                  <input type="text" id="modelName" value="gpt-4o"
                    class="w-full px-2.5 py-1.5 rounded bg-slate-900 border border-slate-800 text-xs text-slate-200 font-mono focus:outline-none focus:border-blue-500">
                </div>
              </div>
              <div>
                <label class="block text-[11px] text-slate-400 mb-1">最大反思迭代轮次: <span id="iterVal" class="text-blue-400 font-bold">3</span> 轮</label>
                <input type="range" id="maxIter" min="1" max="8" value="3" oninput="document.getElementById('iterVal').innerText = this.value"
                  class="w-full accent-blue-500">
              </div>
            </div>
          </details>

          <!-- 操作按钮区 -->
          <div class="pt-2 flex items-center gap-3">
            <button type="submit" id="btnStart"
              class="flex-1 py-2.5 px-4 rounded-lg bg-gradient-to-r from-blue-600 to-cyan-600 hover:from-blue-500 hover:to-cyan-500 text-white font-semibold text-xs shadow-lg shadow-blue-500/20 transition flex items-center justify-center gap-2">
              <span>🚀</span> 启动自愈修复
            </button>
            <button type="button" id="btnStop" onclick="stopTask()" disabled
              class="py-2.5 px-4 rounded-lg bg-slate-800 hover:bg-red-600/80 disabled:opacity-40 disabled:hover:bg-slate-800 text-slate-300 hover:text-white font-semibold text-xs border border-slate-700 transition">
              停止
            </button>
          </div>
        </form>
      </div>

      <!-- 快速架构小说明卡片 -->
      <div class="bg-slate-900/50 border border-slate-800/80 rounded-xl p-4 text-xs text-slate-400 space-y-2">
        <div class="font-medium text-slate-300 flex items-center gap-1.5">
          <span>💡</span> 怎么运行它？
        </div>
        <ul class="list-disc list-inside space-y-1 text-[11px] text-slate-400">
          <li>点击右上角 <strong class="text-slate-300">"预填内置 Demo"</strong>，将自动填充沙箱计算器测试用例；</li>
          <li>点击 <strong class="text-blue-400">"启动自愈修复"</strong>，Agent 将实时走通状态机并向右侧面板输出反馈；</li>
          <li>单测全绿通过后，将在右侧直接查看统一代码差异补丁 (Git Unified Diff)。</li>
        </ul>
      </div>
    </section>

    <!-- 右侧：工作流状态机 + 实时输出面板 (7列) -->
    <section class="lg:col-span-7 space-y-5">
      
      <!-- 状态机流程图 (Stepper) -->
      <div class="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-xl">
        <div class="flex items-center justify-between mb-3">
          <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">LangGraph 状态机拓扑</span>
          <span id="iterBadge" class="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">轮次: 0 / 0</span>
        </div>
        <div class="grid grid-cols-5 gap-2 text-center text-xs">
          <!-- Localize -->
          <div id="step-localize" class="p-2.5 rounded-lg border border-slate-800 bg-slate-950/70 text-slate-400 transition-all duration-300">
            <div class="text-base mb-1">🔍</div>
            <div class="font-bold text-[11px]">Localize</div>
            <div class="text-[10px] text-slate-500 scale-90">AST 嫌疑定位</div>
          </div>
          <!-- Plan -->
          <div id="step-plan" class="p-2.5 rounded-lg border border-slate-800 bg-slate-950/70 text-slate-400 transition-all duration-300">
            <div class="text-base mb-1">📐</div>
            <div class="font-bold text-[11px]">Plan</div>
            <div class="text-[10px] text-slate-500 scale-90">修复方案推演</div>
          </div>
          <!-- Patch -->
          <div id="step-patch" class="p-2.5 rounded-lg border border-slate-800 bg-slate-950/70 text-slate-400 transition-all duration-300">
            <div class="text-base mb-1">🛠️</div>
            <div class="font-bold text-[11px]">Patch</div>
            <div class="text-[10px] text-slate-500 scale-90">精确代码替换</div>
          </div>
          <!-- Test -->
          <div id="step-test" class="p-2.5 rounded-lg border border-slate-800 bg-slate-950/70 text-slate-400 transition-all duration-300">
            <div class="text-base mb-1">🧪</div>
            <div class="font-bold text-[11px]">Test</div>
            <div class="text-[10px] text-slate-500 scale-90">沙箱单测验证</div>
          </div>
          <!-- Reflect -->
          <div id="step-reflect" class="p-2.5 rounded-lg border border-slate-800 bg-slate-950/70 text-slate-400 transition-all duration-300">
            <div class="text-base mb-1">🔁</div>
            <div class="font-bold text-[11px]">Reflect</div>
            <div class="text-[10px] text-slate-500 scale-90">失败反思回滚</div>
          </div>
        </div>
      </div>

      <!-- 实时日志与结果展示 Tab -->
      <div class="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden shadow-xl flex flex-col h-[520px]">
        <!-- 标签栏 -->
        <div class="border-b border-slate-800 bg-slate-950/60 px-4 py-2 flex items-center justify-between">
          <div class="flex items-center space-x-2">
            <button onclick="switchTab('logs')" id="tabBtnLogs"
              class="px-3 py-1 rounded text-xs font-semibold bg-slate-800 text-blue-400 border border-slate-700">
              📟 终端实时日志
            </button>
            <button onclick="switchTab('diff')" id="tabBtnDiff"
              class="px-3 py-1 rounded text-xs font-semibold text-slate-400 hover:text-slate-200">
              📋 最终 Git 补丁 (Diff)
            </button>
            <button onclick="switchTab('reflect')" id="tabBtnReflect"
              class="px-3 py-1 rounded text-xs font-semibold text-slate-400 hover:text-slate-200">
              🧠 历次反思沉淀
            </button>
          </div>
          <button onclick="clearLogs()" class="text-[11px] text-slate-500 hover:text-slate-300">清屏</button>
        </div>

        <!-- 终端日志内容 -->
        <div id="viewLogs" class="flex-1 p-4 overflow-y-auto font-mono text-xs custom-scroll bg-slate-950 text-slate-300 space-y-1">
          <div class="text-slate-600">// 等待任务启动... 准备接收智能体执行链路日志</div>
        </div>

        <!-- Git Diff 内容 -->
        <div id="viewDiff" class="flex-1 p-4 overflow-y-auto font-mono text-xs custom-scroll bg-slate-950 text-emerald-400 hidden">
          <pre id="diffCode" class="whitespace-pre-wrap text-slate-500">// 任务完成后此处将展示生成的标准 Unified Git Diff 补丁文件</pre>
        </div>

        <!-- 反思记录内容 -->
        <div id="viewReflect" class="flex-1 p-4 overflow-y-auto text-xs custom-scroll bg-slate-950 text-slate-300 space-y-2 hidden">
          <div id="reflectList" class="text-slate-500">// 任务执行过程中的单测报错归因与自我纠正经验将展示在此</div>
        </div>
      </div>
    </section>

  </main>

  <script>
    let pollInterval = null;
    let activeTab = 'logs';

    function fillBusinessDemo() {
      document.getElementById('repoPath').value = "D:\\\\Antigravity\\\\demo_business_repo";
      document.getElementById('issueText').value = "order_service.py 计算逻辑存在3处Bug：1. 满减券正好达到门槛(>=)时未享受满减；2. VIP 会员折扣率算错(9折错算成了1折)；3. 优惠券超限抵扣时金额未做 0 元保底导致负数。";
      document.getElementById('testCmd').value = "python test_order.py";
      document.getElementById('maxIter').value = "3";
      document.getElementById('iterVal').innerText = "3";
    }

    function fillDemoParams() {
      document.getElementById('repoPath').value = "内置微型演示沙箱 (临时目录自动生成)";
      document.getElementById('issueText').value = "calculator.py 中的 calculate_discount 函数在传入 20.0% 折扣时计算正确，但对于 0.0 折扣缺少完备测试支持。";
      document.getElementById('testCmd').value = "python test_calc.py";
      document.getElementById('maxIter').value = "3";
      document.getElementById('iterVal').innerText = "3";
    }

    function switchTab(tab) {
      activeTab = tab;
      const tabs = ['logs', 'diff', 'reflect'];
      tabs.forEach(t => {
        const btn = document.getElementById('tabBtn' + t.charAt(0).toUpperCase() + t.slice(1));
        const view = document.getElementById('view' + t.charAt(0).toUpperCase() + t.slice(1));
        if (t === tab) {
          btn.className = "px-3 py-1 rounded text-xs font-semibold bg-slate-800 text-blue-400 border border-slate-700";
          view.classList.remove('hidden');
        } else {
          btn.className = "px-3 py-1 rounded text-xs font-semibold text-slate-400 hover:text-slate-200";
          view.classList.add('hidden');
        }
      });
    }

    function clearLogs() {
      document.getElementById('viewLogs').innerHTML = '<div class="text-slate-600">// 控制台已清空</div>';
    }

    async function handleFormSubmit(e) {
      e.preventDefault();
      const repoPath = document.getElementById('repoPath').value.trim();
      const issueText = document.getElementById('issueText').value.trim();
      const testCmd = document.getElementById('testCmd').value.trim();
      const maxIter = parseInt(document.getElementById('maxIter').value, 10);
      const apiKey = document.getElementById('apiKey').value.trim();
      const baseUrl = document.getElementById('baseUrl').value.trim();
      const modelName = document.getElementById('modelName').value.trim() || 'gpt-4o';

      const isDemo = repoPath.includes("内置微型演示沙箱");

      document.getElementById('btnStart').disabled = true;
      document.getElementById('btnStart').innerHTML = "<span>⏳</span> 正在执行自愈...";
      document.getElementById('btnStop').disabled = false;

      updateStatusUI('running', '自愈修复中...', 1, maxIter);

      try {
        const res = await fetch('/api/run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            is_demo: isDemo,
            repo_path: repoPath,
            issue_text: issueText,
            test_cmd: testCmd,
            max_iterations: maxIter,
            api_key: apiKey,
            base_url: baseUrl,
            model_name: modelName
          })
        });
        const data = await res.json();
        if (data.status === 'started') {
          startPolling();
        } else {
          alert('任务启动失败: ' + (data.message || '未知错误'));
          resetButtons();
        }
      } catch (err) {
        alert('网络请求异常: ' + err.message);
        resetButtons();
      }
    }

    async function stopTask() {
      try {
        await fetch('/api/stop', { method: 'POST' });
      } catch (err) {
        console.error(err);
      }
    }

    function startPolling() {
      if (pollInterval) clearInterval(pollInterval);
      pollInterval = setInterval(fetchStatus, 800);
    }

    async function fetchStatus() {
      try {
        const res = await fetch('/api/status');
        const state = await res.json();

        // 渲染日志
        const logsDiv = document.getElementById('viewLogs');
        if (state.logs && state.logs.length > 0) {
          logsDiv.innerHTML = state.logs.map(log => {
            let color = "text-slate-300";
            if (log.includes("[ERROR]") || log.includes("❌") || log.includes("💥")) color = "text-red-400";
            else if (log.includes("✅") || log.includes("🎉")) color = "text-emerald-400 font-semibold";
            else if (log.includes("[WARNING]") || log.includes("⚠️")) color = "text-amber-400";
            else if (log.includes("🚀") || log.includes("📍")) color = "text-cyan-400";
            return `<div class="${color}">${escapeHtml(log)}</div>`;
          }).join('');
          logsDiv.scrollTop = logsDiv.scrollHeight;
        }

        // 渲染状态机步骤高亮
        highlightStep(state.current_step);

        // 渲染轮次
        document.getElementById('iterBadge').innerText = `轮次: ${state.iteration} / ${state.max_iterations}`;

        // 渲染 Diff
        if (state.git_diff) {
          document.getElementById('diffCode').innerText = state.git_diff;
          document.getElementById('diffCode').className = "whitespace-pre-wrap text-emerald-400 font-mono";
        }

        // 渲染反思
        if (state.reflections && state.reflections.length > 0) {
          document.getElementById('reflectList').innerHTML = state.reflections.map((r, i) => `
            <div class="p-2.5 rounded-lg bg-slate-900 border border-slate-800">
              <span class="text-blue-400 font-bold">#${i+1} 失败复盘:</span> ${escapeHtml(r)}
            </div>
          `).join('');
        }

        // 检查是否结束
        if (!state.is_running && state.status !== 'running') {
          clearInterval(pollInterval);
          pollInterval = null;
          resetButtons();
          if (state.status === 'success') {
            updateStatusUI('success', '自愈修复成功！', state.iteration, state.max_iterations);
            switchTab('diff'); // 成功后自动切换到补丁对比
          } else {
            updateStatusUI('failed', '任务未收敛', state.iteration, state.max_iterations);
          }
        }
      } catch (err) {
        console.error("轮询状态异常:", err);
      }
    }

    function highlightStep(stepName) {
      const steps = ['localize', 'plan', 'patch', 'test', 'reflect'];
      steps.forEach(s => {
        const el = document.getElementById('step-' + s);
        if (s === stepName) {
          el.className = "p-2.5 rounded-lg border-2 border-blue-500 bg-blue-950/60 text-white step-active transition-all duration-300";
        } else {
          el.className = "p-2.5 rounded-lg border border-slate-800 bg-slate-950/70 text-slate-400 transition-all duration-300";
        }
      });
    }

    function updateStatusUI(type, label, iter, maxIter) {
      const badge = document.getElementById('statusBadge');
      if (type === 'running') {
        badge.className = "px-3 py-1.5 rounded-lg text-xs font-semibold bg-blue-500/10 text-blue-400 border border-blue-500/30 flex items-center gap-1.5";
        badge.innerHTML = `<span class="w-2 h-2 rounded-full bg-blue-500 animate-ping"></span> ${label}`;
      } else if (type === 'success') {
        badge.className = "px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 flex items-center gap-1.5";
        badge.innerHTML = `<span class="w-2 h-2 rounded-full bg-emerald-500"></span> ${label}`;
      } else if (type === 'failed') {
        badge.className = "px-3 py-1.5 rounded-lg text-xs font-semibold bg-red-500/10 text-red-400 border border-red-500/30 flex items-center gap-1.5";
        badge.innerHTML = `<span class="w-2 h-2 rounded-full bg-red-500"></span> ${label}`;
      } else {
        badge.className = "px-3 py-1.5 rounded-lg text-xs font-semibold bg-slate-800 text-slate-400 border border-slate-700 flex items-center gap-1.5";
        badge.innerHTML = `<span class="w-2 h-2 rounded-full bg-slate-500"></span> 空闲就绪`;
      }
    }

    function resetButtons() {
      const btn = document.getElementById('btnStart');
      btn.disabled = false;
      btn.innerHTML = "<span>🚀</span> 启动自愈修复";
      document.getElementById('btnStop').disabled = true;
    }

    function escapeHtml(str) {
      return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }
  </script>
</body>
</html>
"""


class GitHealerRequestHandler(BaseHTTPRequestHandler):
    """处理前端 REST API 与单页面 HTML 托管"""

    def _send_json(self, status_code: int, data: Dict[str, Any]):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            content = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif self.path == "/api/status":
            self._send_json(200, task_mgr.get_snapshot())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/run":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            payload = json.loads(body) if body else {}

            if task_mgr.is_running:
                self._send_json(400, {"status": "error", "message": "当前已有任务正在执行中"})
                return

            max_iter = int(payload.get("max_iterations", 3))
            task_mgr.reset(max_iter=max_iter)

            is_demo = payload.get("is_demo", False)

            def runner():
                if is_demo:
                    # 运行演示沙箱
                    with tempfile.TemporaryDirectory() as temp_dir:
                        # 真正包含 Bug 的原代码：未处理大于 1.0 的百分比输入 (如 20.0 代表 20%)
                        buggy_code = '''def calculate_discount(price: float, discount_rate: float) -> float:
    """计算折扣价格（缺陷：未适配 20.0 这种百分比形式的折扣输入）"""
    return price * (1.0 - discount_rate)
'''
                        calc_path = os.path.join(temp_dir, "calculator.py")
                        with open(calc_path, "w", encoding="utf-8") as f:
                            f.write(buggy_code)

                        test_script = '''import sys
from calculator import calculate_discount

assert calculate_discount(100.0, 0.2) == 80.0, "Test 1 Failed"
assert calculate_discount(100.0, 20.0) == 80.0, "Test 2 Failed"
assert calculate_discount(50.0, 0.0) == 50.0, "Test 3 Failed"
print("ALL TESTS PASSED SUCCESSFULLY!")
'''
                        test_path = os.path.join(temp_dir, "test_calc.py")
                        with open(test_path, "w", encoding="utf-8") as f:
                            f.write(test_script)

                        execute_healing_task(
                            repo_path=temp_dir,
                            issue_text=payload.get("issue_text", "calculator.py 折扣计算校验"),
                            test_cmd=f"{sys.executable} test_calc.py",
                            max_iterations=max_iter,
                            api_key=payload.get("api_key", ""),
                            base_url=payload.get("base_url", ""),
                            model_name=payload.get("model_name", "gpt-4o")
                        )
                else:
                    execute_healing_task(
                        repo_path=payload.get("repo_path", "."),
                        issue_text=payload.get("issue_text", ""),
                        test_cmd=payload.get("test_cmd", "pytest"),
                        max_iterations=max_iter,
                        api_key=payload.get("api_key", ""),
                        base_url=payload.get("base_url", ""),
                        model_name=payload.get("model_name", "gpt-4o")
                    )

            t = threading.Thread(target=runner, daemon=True)
            t.start()
            self._send_json(200, {"status": "started"})

        elif self.path == "/api/stop":
            task_mgr.should_stop = True
            self._send_json(200, {"status": "stopping"})
        else:
            self.send_response(404)
            self.end_headers()


def start_server(host: str = "127.0.0.1", port: int = 7860):
    server = HTTPServer((host, port), GitHealerRequestHandler)
    print("=" * 65)
    print(f"  🌐 GitHealer 可视化 Web 控制台已成功启动！")
    print(f"  👉 浏览器访问地址: http://{host}:{port}")
    print("=" * 65)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[!] 正在停止 Web 服务...")
        server.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="GitHealer Web Dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="绑定监听 Host (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7860, help="绑定端口 (默认 7860)")
    args = parser.parse_args()
    start_server(host=args.host, port=args.port)
