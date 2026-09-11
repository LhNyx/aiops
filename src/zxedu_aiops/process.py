"""跨平台进程管理。

关键设计规则：
- 禁止 os.fork()、forkpty()、双重 fork 守护进程化。
- POSIX 分离：subprocess.Popen + start_new_session=True。
- Windows 分离：DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP。
- 平台分支一律用 sys.platform 判断，不猜运行环境。
"""

from __future__ import annotations

import contextlib
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import psutil

# 手写数值而非 subprocess.DETACHED_PROCESS：这两个符号 subprocess 只在
# Windows 版的模块里才导出，写死 hex 让本模块在 macOS/Linux 上也能正常 import
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def spawn_detached(cmd: Sequence[str], log_file: Path) -> int:
    """分离式拉起一个后台进程（父进程退出后它继续活着）。

        Args:
            cmd: 要执行的命令及其参数。
            log_file: stdout/stderr 追加写入的日志文件。

        Returns:
            新进程的 PID。
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)

    kwargs: dict = {
        "stderr": subprocess.STDOUT, # # stderr 并入 stdout，一个日志文件收全
        "close_fds": True, # 不继承父进程的文件描述符，干净
    }

    if sys.platform == "win32":
        # DETACHED_PROCESS：不继承父进程控制台；CREATE_NEW_PROCESS_GROUP：
        # 独立进程组，不吃父终端的 Ctrl+C（POSIX 上对应 SIGHUP 免疫）
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        # 子进程内执行 setsid()：脱离父进程的会话和控制终端——
        # 一个参数顶替整套"双重 fork"仪式
        kwargs["start_new_session"] = True

    fh = log_file.open("ab")
    kwargs["stdout"] = fh
    proc = subprocess.Popen(cmd, **kwargs)
    fh.close() # 父进程侧的句柄立即关掉；子进程继承的是自己的副本，互不影响
    return proc.pid


def write_pid_file(pid_path: Path, pid: int) -> None:
    """把 PID 写入文件。"""
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid), encoding="utf-8")


def read_pid_file(pid_path: Path) -> int | None:
    """从文件读 PID。文件缺失或内容非法时返回 None（不抛异常）。"""
    try:
        text = pid_path.read_text(encoding="utf-8").strip()
        return int(text)
    except (FileNotFoundError, ValueError):
        return None


def remove_pid_file(pid_path: Path) -> None:
    """删除 pid 文件（不存在也不报错）。"""
    with contextlib.suppress(FileNotFoundError, OSError):
        pid_path.unlink(missing_ok=True)


def is_process_alive(pid: int) -> bool:
    """用 psutil 判断给定 PID 的进程是否活着。"""
    try:
        proc = psutil.Process(pid)
        # 僵尸进程在进程表里仍占一行（is_running()==True），但它已经死了
        # ——在等父进程收尸。必须显式排除，否则 stop 逻辑会误判"杀不掉"
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def terminate_process(pid: int, timeout: float = 5.0) -> bool:
    """优雅终止进程，超时后升级为强杀。

    POSIX：先 SIGTERM（可被捕获，给程序清理现场的机会），超时再 SIGKILL。
    Windows：直接 TerminateProcess（Windows 没有信号机制，没有优雅档）。

    成功终止返回 True。
    """
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True  # 已经不在了——幂等语义：重复 stop 不算失败

    # 第一档：优雅终止
    if sys.platform == "win32":
        proc.terminate()  # Windows 上就是 TerminateProcess
    else:
        proc.send_signal(signal.SIGTERM)

    # 给优雅退出留时间
    try:
        proc.wait(timeout=timeout)
        return True
    except psutil.TimeoutExpired:
        pass

    # 第二档：强杀（SIGKILL，不可捕获不可忽略）
    try:
        proc.kill()
        proc.wait(timeout=3.0)
    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
        pass

    return not is_process_alive(pid)


def stop_server(pid_path: Path) -> bool:
    """停止运行中的服务。已停止或本来就没在跑都返回 True。"""
    pid = read_pid_file(pid_path)
    if pid is None:
        remove_pid_file(pid_path)
        return True

    # pid 文件在、进程已经没了 → 陈旧 pid 文件（服务崩了没来得及清理），清掉即可
    if not is_process_alive(pid):
        remove_pid_file(pid_path)
        return True

    success = terminate_process(pid)
    remove_pid_file(pid_path)
    return success


def health_check(host: str, port: int, timeout: float = 2.0) -> bool:
    """探测服务健康端点是否返回 200。"""
    import urllib.error
    import urllib.request

    # 函数内延迟导入：CLI 每次执行都会 import 本模块，保持模块级导入轻量
    url = f"http://{host}:{port}/api/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_for_health(host: str, port: int, timeout: float = 15.0, interval: float = 0.5) -> bool:
    """轮询健康端点，返回 200 即真；超过 timeout 返回假。"""
    # monotonic 单调时钟：不受系统改时间/NTP 校时影响，测"过了多久"必须用它
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health_check(host, port, timeout=2.0):
            return True
        time.sleep(interval)
    return False


def find_process_on_port(port: int) -> int | None:
    """返回正在监听 *port* 的任意进程 PID，没有则 None。

    用 psutil 扫网络连接。Linux 上读的是 /proc/net/tcp*（全局可读），
    无需 root。
    """
    for conn in psutil.net_connections(kind="inet"):
        if conn.status == "LISTEN" and conn.laddr is not None and conn.laddr.port == port and conn.pid is not None:
            return conn.pid
    return None


def print_tail(filepath: Path, lines: int = 10) -> None:
    """把文件最后 *lines* 行打到 stderr（文件不存在则静默）。

    用途：start 失败时给用户看日志尾部，定位崩溃原因。
    """
    import sys as _sys

    if not filepath.exists():
        return
    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")
        all_lines = text.splitlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        for line in tail:
            print(f"    {line}", file=_sys.stderr)
    except OSError:
        pass