# -*- coding: utf-8 -*-
"""
代码仓库语法分析器模块 (AST Indexer)
================================================================================
基于 Tree-sitter 实现轻量级语法树大纲提取、按需源码检索与函数调用层级分析。

核心设计目标：
1. 避免上下文溢出：大模型不需要读入全量无关代码，通过 AST 大纲实现“按需展开”。
2. 语法容错性高：Tree-sitter 原生支持容错解析，即使代码存在语法错误也能解析出大部分类与函数结构。
3. 优雅降级：支持在 Tree-sitter 与 Python 标准库 ast 模块之间无缝自动降级。
"""

import os
import ast
import re
from typing import List, Dict, Any, Optional, Set
from src.core.schemas import SymbolOutline
from src.core.config import AgentConfig

# 尝试载入 Tree-sitter 引擎
try:
    import tree_sitter_languages
    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False


class RepoASTIndexer:
    """使用 Tree-sitter 对代码仓库进行语法级大纲提取和符号定位"""

    def __init__(self, repo_path: str, language: str = "python") -> None:
        """
        初始化代码仓库语法分析器
        :param repo_path: 代码仓库本地绝对路径
        :param language: 目标分析编程语言，默认为 python
        """
        self.repo_path = os.path.abspath(repo_path)
        self.language = language.lower()
        self.parser = None

        # 初始化 Tree-sitter 解析器
        if HAS_TREE_SITTER:
            try:
                self.parser = tree_sitter_languages.get_parser(self.language)
            except Exception:
                self.parser = None

    def _normalize_rel_path(self, rel_path: str) -> str:
        """清洗并格式化相对路径，统一使用正斜杠"""
        cleaned = rel_path.strip().replace("\\", "/")
        if cleaned.startswith("./"):
            cleaned = cleaned[2:]
        elif cleaned.startswith("/"):
            cleaned = cleaned[1:]
        return cleaned

    def get_file_tree(self) -> List[str]:
        """
        扫描代码仓库，提取所有有效源码文件相对路径
        自动过滤 .git, __pycache__, .venv 等配置在 AgentConfig.IGNORE_DIRS 中的目录
        :return: 排序后的相对文件路径列表
        """
        valid_files = []
        ignore_dirs = set(AgentConfig.IGNORE_DIRS)

        # 语言对应常见扩展名
        allowed_extensions = {".py"} if self.language == "python" else {".py", ".js", ".ts", ".go"}

        for root, dirs, files in os.walk(self.repo_path):
            # 过滤忽略目录，防止向下递归
            dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]

            for file in files:
                _, ext = os.path.splitext(file)
                if ext in allowed_extensions:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, self.repo_path)
                    valid_files.append(self._normalize_rel_path(rel_path))

        valid_files.sort()
        return valid_files

    def parse_file_outline(self, rel_path: str) -> List[SymbolOutline]:
        """
        提取目标源码文件的顶层类、函数、方法结构骨架（带起止行号与文档注释）
        优先使用 Tree-sitter，若不可用或报错则自动降级为标准库 ast 解析
        :param rel_path: 目标文件相对路径
        :return: 符号大纲列表
        """
        normalized_path = self._normalize_rel_path(rel_path)
        full_path = os.path.join(self.repo_path, normalized_path)

        if not os.path.exists(full_path) or not os.path.isfile(full_path):
            return []

        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                source_code = f.read()
        except Exception:
            return []

        if not source_code.strip():
            return []

        # 优先使用 Tree-sitter 解析
        if self.parser is not None:
            try:
                return self._parse_with_tree_sitter(source_code)
            except Exception:
                pass  # 若解析失败，静默降级为 ast 解析

        # 降级方案：使用 Python 原生 ast 模块
        if self.language == "python":
            return self._parse_with_python_ast(source_code)

        return []

    def _parse_with_tree_sitter(self, source_code: str) -> List[SymbolOutline]:
        """使用 Tree-sitter 进行 AST 遍历解析"""
        byte_code = source_code.encode("utf-8")
        tree = self.parser.parse(byte_code)
        root = tree.root_node

        outlines: List[SymbolOutline] = []

        def _extract_docstring(body_node) -> Optional[str]:
            """从函数或类的 body 节点提取首个字符串表达式作为 docstring"""
            if not body_node:
                return None
            for child in body_node.children:
                if child.type == "expression_statement":
                    first_child = child.children[0] if child.children else None
                    if first_child and first_child.type == "string":
                        raw_text = first_child.text.decode("utf-8", errors="replace")
                        # 去除三引号或单引号
                        return raw_text.strip("'''\"\"\" \n\r\t")
            return None

        def _traverse(node, parent_class_name: Optional[str] = None):
            for child in node.children:
                if child.type == "class_definition":
                    name_node = child.child_by_field_name("name")
                    class_name = name_node.text.decode("utf-8", errors="replace") if name_node else "AnonymousClass"
                    start_line = child.start_point[0] + 1
                    end_line = child.end_point[0] + 1
                    body_node = child.child_by_field_name("body")
                    doc = _extract_docstring(body_node)

                    outlines.append(
                        SymbolOutline(
                            name=class_name,
                            kind="class",
                            start_line=start_line,
                            end_line=end_line,
                            docstring=doc
                        )
                    )

                    # 递归遍历类内部的方法
                    if body_node:
                        _traverse(body_node, parent_class_name=class_name)

                elif child.type == "function_definition":
                    name_node = child.child_by_field_name("name")
                    func_name = name_node.text.decode("utf-8", errors="replace") if name_node else "anonymous_func"
                    start_line = child.start_point[0] + 1
                    end_line = child.end_point[0] + 1
                    body_node = child.child_by_field_name("body")
                    doc = _extract_docstring(body_node)

                    kind = "method" if parent_class_name else "function"
                    full_name = f"{parent_class_name}.{func_name}" if parent_class_name else func_name

                    outlines.append(
                        SymbolOutline(
                            name=full_name,
                            kind=kind,
                            start_line=start_line,
                            end_line=end_line,
                            docstring=doc
                        )
                    )

        _traverse(root)
        return outlines

    def _parse_with_python_ast(self, source_code: str) -> List[SymbolOutline]:
        """使用 Python 原生 ast 模块解析大纲（降级备用）"""
        outlines: List[SymbolOutline] = []
        try:
            tree = ast.parse(source_code)
        except SyntaxError:
            # 容错：当存在严重语法错误无法解析时返回空
            return []

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                doc = ast.get_docstring(node)
                end_line = getattr(node, "end_lineno", node.lineno)
                outlines.append(
                    SymbolOutline(
                        name=node.name,
                        kind="class",
                        start_line=node.lineno,
                        end_line=end_line,
                        docstring=doc
                    )
                )

                # 遍历类内部定义的方法
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_doc = ast.get_docstring(item)
                        m_end_line = getattr(item, "end_lineno", item.lineno)
                        outlines.append(
                            SymbolOutline(
                                name=f"{node.name}.{item.name}",
                                kind="method",
                                start_line=item.lineno,
                                end_line=m_end_line,
                                docstring=method_doc
                            )
                        )

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node)
                end_line = getattr(node, "end_lineno", node.lineno)
                outlines.append(
                    SymbolOutline(
                        name=node.name,
                        kind="function",
                        start_line=node.lineno,
                        end_line=end_line,
                        docstring=doc
                    )
                )

        return outlines

    def get_symbol_source(self, rel_path: str, symbol_name: str) -> str:
        """
        根据符号名称精准提取该函数或类的完整实现源码（按需展开，节省上下文）
        :param rel_path: 目标文件相对路径
        :param symbol_name: 函数名或类名（支持 'method' 或 'Class.method'）
        :return: 该符号对应的源码文本
        """
        normalized_path = self._normalize_rel_path(rel_path)
        full_path = os.path.join(self.repo_path, normalized_path)

        if not os.path.exists(full_path):
            raise FileNotFoundError(f"未找到目标文件: {normalized_path}")

        outlines = self.parse_file_outline(normalized_path)
        target_outline = None0

        # 匹配符号：支持精准名称或末尾方法名匹配
        for outline in outlines:
            if outline.name == symbol_name or outline.name.endswith(f".{symbol_name}"):
                target_outline = outline
                break

        if not target_outline:
            raise ValueError(f"在文件 {normalized_path} 中未找到符号: {symbol_name}")

        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        # 切片提取（1-indexed 转换为 0-indexed）
        start_idx = max(0, target_outline.start_line - 1)
        end_idx = min(len(lines), target_outline.end_line)
        selected_lines = lines[start_idx:end_idx]

        return "".join(selected_lines)

    def search_symbol(self, query: str) -> List[Dict[str, Any]]:
        """
        在全仓库范围内检索符号定义与引用的位置
        :param query: 待搜索的符号名或关键词
        :return: 匹配到的符号详情列表
        """
        results: List[Dict[str, Any]] = []
        file_tree = self.get_file_tree()

        for rel_path in file_tree:
            outlines = self.parse_file_outline(rel_path)
            for outline in outlines:
                if outline.matches_query(query):
                    results.append({
                        "file_path": rel_path,
                        "symbol": outline.model_dump(),
                        "preview": outline.to_compact_string()
                    })

        return results

    def get_call_hierarchy(self, rel_path: str, symbol_name: str) -> Dict[str, List[str]]:
        """
        基于 AST 调用分析，解析该函数的上游调用者（Callers）与下游被调用者（Callees）
        :param rel_path: 文件相对路径
        :param symbol_name: 目标函数名
        :return: 包含 callers 和 callees 列表的字典
        """
        normalized_path = self._normalize_rel_path(rel_path)
        callers: Set[str] = set()
        callees: Set[str] = set()

        # 提取目标符号的基础函数名 (去除类前缀)
        base_name = symbol_name.split(".")[-1]

        # 1. 解析目标符号内部调用了谁 (Callees)
        try:
            symbol_code = self.get_symbol_source(normalized_path, symbol_name)
            # 使用正则和 ast 快速提取函数调用节点
            try:
                tree = ast.parse(symbol_code)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        if isinstance(node.func, ast.Name):
                            callees.add(node.func.id)
                        elif isinstance(node.func, ast.Attribute):
                            callees.add(node.func.attr)
            except SyntaxError:
                # 备用正则提取
                call_matches = re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", symbol_code)
                callees.update(call_matches)
        except Exception:
            pass

        # 2. 全库检索谁调用了该符号 (Callers)
        call_pattern = re.compile(rf"\b{re.escape(base_name)}\s*\(")
        all_files = self.get_file_tree()

        for fpath in all_files:
            full_path = os.path.join(self.repo_path, fpath)
            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()

                # 如果文件中包含对该符号的调用
                if call_pattern.search(content):
                    outlines = self.parse_file_outline(fpath)
                    for outline in outlines:
                        # 排除自身定义
                        if outline.name == symbol_name and fpath == normalized_path:
                            continue
                        try:
                            func_code = self.get_symbol_source(fpath, outline.name)
                            if call_pattern.search(func_code):
                                callers.add(f"{fpath}::{outline.name}")
                        except Exception:
                            continue
            except Exception:
                continue

        return {
            "callers": sorted(list(callers)),
            "callees": sorted(list(callees))
        }
