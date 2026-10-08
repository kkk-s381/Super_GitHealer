# -*- coding: utf-8 -*-
"""
精准代码补丁与版本控制模块 (Code Editor)
================================================================================
负责安全的代码块替换、唯一性前置校验、版本快照以及一键故障回滚。

核心设计原则：
1. 块级替换（Search-and-Replace）：拒绝整文件重写，只替换目标片段，杜绝幻觉遗漏。
2. 严格唯一性检查：如果 old_str 出现 0 次或 >1 次，立即抛出明确异常阻止误伤。
3. 双模快照与回滚（Dual-Mode Checkpoint）：
   - 若检测到当前目录为 Git 仓库，优先结合 Git 与暂存机制；
   - 若非 Git 仓库，自动启用纯文件级快照恢复，保证无 Git 环境下 100% 正常回滚。
4. 换行符自适应：自动适配 Windows (CRLF) 与 Linux (LF)，防止因换行符差异导致匹配失败。
"""

import os
import time
import uuid
import difflib
import subprocess
from typing import Dict, List, Optional, Any
from src.core.schemas import FileEditAction


class CodeEditor:
    """负责安全的代码文件修改、唯一性校验、版本快照以及一键回滚"""

    def __init__(self, repo_path: str) -> None:
        """
        初始化代码编辑器
        :param repo_path: 代码仓库根目录路径
        """
        self.repo_path = os.path.abspath(repo_path)
        if not os.path.exists(self.repo_path):
            raise FileNotFoundError(f"指定的代码仓库路径不存在: {self.repo_path}")

        self.is_git_repo = self._check_git_repo()

        # 内存快照仓库: checkpoint_id -> { rel_file_path: file_content_str }
        self._checkpoints: Dict[str, Dict[str, str]] = {}
        # 初始基线文件备份: rel_file_path -> original_content_str
        self._baseline_files: Dict[str, str] = {}
        # 快照有序历史
        self._checkpoint_history: List[str] = []

    def _check_git_repo(self) -> bool:
        """检查当前仓库是否处于 Git 版本控制之下"""
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=self.repo_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False
            )
            return res.returncode == 0 and res.stdout.strip() == "true"
        except Exception:
            return False

    def _normalize_rel_path(self, rel_path: str) -> str:
        """规范化相对路径，使用统一的正斜杠"""
        cleaned = rel_path.strip().replace("\\", "/")
        if cleaned.startswith("./"):
            cleaned = cleaned[2:]
        elif cleaned.startswith("/"):
            cleaned = cleaned[1:]
        return cleaned

    def read_file(self, rel_path: str) -> str:
        """读取指定相对路径的文件内容"""
        full_path = os.path.join(self.repo_path, self._normalize_rel_path(rel_path))
        if not os.path.exists(full_path):
            return ""
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def create_checkpoint(self) -> str:
        """
        在执行任何修改前创建快照，支持无缝一键回滚
        :return: 快照唯一标识符 (checkpoint_id)
        """
        ckpt_id = f"ckpt_{len(self._checkpoint_history) + 1}_{int(time.time())}_{uuid.uuid4().hex[:4]}"

        # 捕获当前所有已记录/可能被修改文件的状态
        snapshot: Dict[str, str] = {}
        for rel_path in self._baseline_files.keys():
            full_path = os.path.join(self.repo_path, rel_path)
            if os.path.exists(full_path):
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    snapshot[rel_path] = f.read()

        self._checkpoints[ckpt_id] = snapshot
        self._checkpoint_history.append(ckpt_id)
        return ckpt_id

    def apply_replace_patch(self, action: FileEditAction) -> bool:
        """
        执行基于精确代码块的 Search-and-Replace 修改
        前置校验规则：
        1. 检查 old_str 是否在目标文件中唯一存在
        2. 若不存在或匹配到多处，抛出异常阻止错误改写
        3. 自适应换行符处理（CRLF / LF）
        :param action: 替换动作契约
        :return: 修改是否成功写入
        """
        rel_path = self._normalize_rel_path(action.file_path)
        full_path = os.path.join(self.repo_path, rel_path)

        if not os.path.exists(full_path):
            raise FileNotFoundError(f"目标文件不存在: {rel_path}")

        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            raw_content = f.read()

        # 记录初始基线（用于后续计算全局 Diff）
        if rel_path not in self._baseline_files:
            self._baseline_files[rel_path] = raw_content

        # 针对每个快照补充当前文件的备份
        for snapshot in self._checkpoints.values():
            if rel_path not in snapshot:
                snapshot[rel_path] = raw_content

        # 换行符自适应归一化为 \n
        has_crlf = "\r\n" in raw_content
        norm_content = raw_content.replace("\r\n", "\n")
        norm_old_str = action.old_str.replace("\r\n", "\n")
        norm_new_str = action.new_str.replace("\r\n", "\n")

        # 严格唯一性检查
        match_count = norm_content.count(norm_old_str)
        if match_count == 0:
            # 容错匹配 1：去除每行末尾空格后再尝试行级匹配
            lines_content = norm_content.splitlines()
            lines_old = norm_old_str.splitlines()
            lines_content_stripped = [l.rstrip() for l in lines_content]
            lines_old_stripped = [l.rstrip() for l in lines_old]

            matched_indices = []
            len_old = len(lines_old_stripped)
            if len_old > 0 and len(lines_content_stripped) >= len_old:
                for i in range(len(lines_content_stripped) - len_old + 1):
                    if lines_content_stripped[i:i + len_old] == lines_old_stripped:
                        matched_indices.append(i)

            if len(matched_indices) == 1:
                # 唯一容错匹配成功，使用行切片执行替换
                start_i = matched_indices[0]
                end_i = start_i + len_old
                lines_new = norm_new_str.splitlines()
                replaced_lines = lines_content[:start_i] + lines_new + lines_content[end_i:]
                norm_content = "\n".join(replaced_lines)
                match_count = 1
                norm_old_str = None  # 标记已完成替换
            else:
                raise ValueError(
                    f"代码定位失败：在 '{rel_path}' 中未找到与 old_str 完全精确匹配的代码块。\n"
                    f"【待匹配片段前 80 字符】: {repr(action.old_str[:80])}\n"
                    f"提示：请通过源码重新确认目标代码的当前精确内容、变量名与缩进格式。"
                )
        if match_count > 1:
            raise ValueError(
                f"代码定位歧义：在 '{rel_path}' 中匹配到了 {match_count} 处完全相同的代码块。\n"
                f"为了避免误伤非目标代码，请在 old_str 中包含更多外层函数名或上下文行以确保定位唯一性。"
            )

        # 执行替换（若前面行级容错已替换则跳过）
        if norm_old_str is not None:
            replaced_content = norm_content.replace(norm_old_str, norm_new_str, 1)
        else:
            replaced_content = norm_content

        # 恢复原文件对应的换行符格式
        final_content = replaced_content.replace("\n", "\r\n") if has_crlf else replaced_content

        # 写入磁盘
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(final_content)

        return True

    def rollback_to_checkpoint(self, checkpoint_id: str) -> bool:
        """
        单测严重报错或语法崩溃时，将工作区代码一键还原至指定快照
        :param checkpoint_id: 快照标识符
        :return: 回滚是否成功
        """
        if checkpoint_id not in self._checkpoints:
            return False

        snapshot = self._checkpoints[checkpoint_id]
        for rel_path, content in snapshot.items():
            full_path = os.path.join(self.repo_path, rel_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)

        # 若是 Git 仓库，同步清理可能生成的未跟踪中间文件
        if self.is_git_repo:
            try:
                subprocess.run(
                    ["git", "clean", "-fd"],
                    cwd=self.repo_path,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False
                )
            except Exception:
                pass

        return True

    def get_unified_diff(self) -> str:
        """
        获取当前工作区相比最初状态的完整 Git Patch (Diff)
        若在 Git 仓库中优先采用 `git diff`，否则使用 `difflib` 基于基线生成标准 Diff
        :return: 标准统一格式的 Git Diff 文本
        """
        # 1. 尝试使用 Git 原生 diff
        if self.is_git_repo:
            try:
                res = subprocess.run(
                    ["git", "diff", "HEAD"],
                    cwd=self.repo_path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False
                )
                if res.returncode == 0 and res.stdout.strip():
                    return res.stdout
            except Exception:
                pass

        # 2. 纯文件级 Difflib 备用生成
        all_diffs: List[str] = []
        for rel_path, original_content in self._baseline_files.items():
            full_path = os.path.join(self.repo_path, rel_path)
            if not os.path.exists(full_path):
                current_content = ""
            else:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    current_content = f.read()

            if original_content == current_content:
                continue

            old_lines = original_content.replace("\r\n", "\n").splitlines(keepends=True)
            new_lines = current_content.replace("\r\n", "\n").splitlines(keepends=True)

            diff = difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
                lineterm=""
            )
            all_diffs.append("".join(diff))

        return "\n".join(all_diffs)

    def compute_patch_hash(self, action: FileEditAction) -> str:
        """
        计算本次修改动作的摘要 Hash，用于死循环横跳检测
        :param action: 替换动作
        :return: SHA256 摘要字符串
        """
        return action.compute_patch_hash()

    def create_new_file(self, rel_path: str, content: str) -> bool:
        """
        辅助接口：若 Agent 需要新建测试文件或辅助脚本
        :param rel_path: 相对路径
        :param content: 文件内容
        :return: 是否创建成功
        """
        target_path = self._normalize_rel_path(rel_path)
        full_path = os.path.join(self.repo_path, target_path)

        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        if target_path not in self._baseline_files and not os.path.exists(full_path):
            self._baseline_files[target_path] = ""

        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        return True

    def list_checkpoints(self) -> List[str]:
        """列出当前所有已保存的快照 ID 列表"""
        return list(self._checkpoint_history)
