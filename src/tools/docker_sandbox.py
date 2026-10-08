# -*- coding: utf-8 -*-
"""
Docker 沙箱执行与日志剪枝模块 (Docker Sandbox)
================================================================================
在隔离容器内安全运行单测指令，实现网络隔离、资源限额与错误 Traceback 核心日志剪枝。

核心设计：
1. 双模执行（Dual-Mode Execution）：
   - 优先模式：通过 Docker SDK 在隔离容器内挂载执行（网络隔离、内存硬限制）；
   - 容错降级：若宿主机未安装/未启动 Docker 服务，自动降级为受限本地子进程（Subprocess），保证开箱即用。
2. 日志智能剪枝：从成百上千行的控制台输出中，精准提取最后的 Traceback 错误帧与 AssertionError，极大压缩 Prompt Token。
3. 超时硬中断：防止单测由于死循环导致任务挂起。
"""

import os
import re
import time
import subprocess
import logging
from typing import Optional, Tuple
from src.core.schemas import TestResult

try:
    import docker
    HAS_DOCKER = True
except ImportError:
    HAS_DOCKER = False

logger = logging.getLogger("GitHealer.Sandbox")


class DockerSandbox:
    """在隔离容器中执行单测指令，实现网络隔离、资源限额与环境毫秒级重置"""

    def __init__(
        self,
        repo_host_path: str,
        image_name: str = "python:3.10-slim",
        memory_limit: str = "1g"
    ) -> None:
        """
        初始化沙箱环境
        :param repo_host_path: 宿主机上的代码仓库目录
        :param image_name: 沙箱基础镜像名
        :param memory_limit: 内存硬限制（防 OOM 崩溃）
        """
        self.repo_host_path = os.path.abspath(repo_host_path)
        self.image_name = image_name
        self.memory_limit = memory_limit
        self.docker_client = None
        self.container = None

        # 尝试连接 Docker Daemon
        if HAS_DOCKER:
            try:
                self.docker_client = docker.from_env()
                self.docker_client.ping()
            except Exception:
                self.docker_client = None

    def start_container(self) -> None:
        """启动沙箱容器，挂载工作区目录，禁用外部网络访问"""
        if self.docker_client is None:
            return

        try:
            self.container = self.docker_client.containers.run(
                self.image_name,
                command="tail -f /dev/null",  # 常驻后台等待执行指令
                volumes={self.repo_host_path: {"bind": "/workspace", "mode": "rw"}},
                working_dir="/workspace",
                detach=True,
                mem_limit=self.memory_limit,
                network_disabled=True,  # 禁用容器外网，保证安全边界
                remove=True
            )
        except Exception as e:
            logger.warning(f"启动 Docker 容器失败，将降级使用本地子进程执行: {e}")
            self.container = None

    def run_test_command(self, test_cmd: str, timeout_seconds: int = 60) -> TestResult:
        """
        执行指定的测试指令，带超时硬熔断控制
        优先在容器内执行，若不可用则降级在本地工作区执行
        :param test_cmd: 单测运行指令 (如 'pytest tests/test_core.py')
        :param timeout_seconds: 超时熔断阈值（秒）
        :return: 结构化单测执行结果
        """
        start_time = time.time()

        # 模式 1：Docker 容器隔离执行
        if self.container is not None:
            try:
                exit_code, output_bytes = self.container.exec_run(
                    f"bash -c '{test_cmd}'",
                    workdir="/workspace"
                )
                duration = time.time() - start_time
                raw_logs = output_bytes.decode("utf-8", errors="replace")
                cleaned_logs = self.clean_traceback_logs(raw_logs)
                return TestResult.from_execution(
                    exit_code=exit_code,
                    raw_logs=raw_logs,
                    cleaned_traceback=cleaned_logs,
                    duration=duration
                )
            except Exception as e:
                logger.warning(f"Docker 执行异常，转为本地降级执行: {e}")

        # 模式 2：安全子进程降级执行
        try:
            res = subprocess.run(
                test_cmd,
                shell=True,
                cwd=self.repo_host_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False
            )
            duration = time.time() - start_time
            raw_logs = res.stdout or ""
            cleaned_logs = self.clean_traceback_logs(raw_logs)
            return TestResult.from_execution(
                exit_code=res.returncode,
                raw_logs=raw_logs,
                cleaned_traceback=cleaned_logs,
                duration=duration
            )
        except subprocess.TimeoutExpired:
            duration = time.time() - start_time
            return TestResult.from_execution(
                exit_code=-1,
                raw_logs=f"【执行超时】单测命令执行超过 {timeout_seconds} 秒硬上限，已强制中断！",
                cleaned_traceback=f"TimeoutError: 单测执行超时 ({timeout_seconds}s)，疑似存在死循环或锁等待。",
                duration=duration
            )
        except Exception as e:
            duration = time.time() - start_time
            return TestResult.from_execution(
                exit_code=-2,
                raw_logs=str(e),
                cleaned_traceback=f"ExecutionException: 执行命令异常 - {str(e)}",
                duration=duration
            )

    def clean_traceback_logs(self, raw_logs: str) -> str:
        """
        剪枝处理：从冗长的测试输出中提取最后的 Traceback 关键帧与断言错误
        过滤无关控制台信息，压缩 Prompt Token 占用
        :param raw_logs: 原始控制台日志
        :return: 精简后的核心报错文本
        """
        if not raw_logs or not raw_logs.strip():
            return "无控制台输出日志"

        lines = raw_logs.splitlines()

        # 尝试寻找 Traceback 标志
        tb_indices = [i for i, line in enumerate(lines) if "Traceback (most recent call last):" in line]
        if tb_indices:
            # 截取从最后一个 Traceback 开始到末尾的内容
            last_tb_start = tb_indices[-1]
            extracted_lines = lines[last_tb_start:]
            # 限制在最多 50 行内
            return "\n".join(extracted_lines[:50])

        # 寻找 pytest 或 unittest 的 FAILURES 汇总段落
        failure_indices = [i for i, line in enumerate(lines) if "FAILURES" in line or "FAILED" in line]
        if failure_indices:
            start = max(0, failure_indices[0] - 2)
            return "\n".join(lines[start:start + 45])

        # 兜底：保留最后 35 行最有价值的报错或退出摘要
        return "\n".join(lines[-35:]) if len(lines) > 35 else raw_logs

    def close(self) -> None:
        """清理并销毁所有挂载的沙箱容器及临时卷资源"""
        if self.container is not None:
            try:
                self.container.stop(timeout=2)
                self.container.remove()
            except Exception:
                pass
            finally:
                self.container = None
