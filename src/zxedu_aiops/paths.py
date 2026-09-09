"""ZXEDU 主目录的路径解析。

项目里所有落盘位置的唯一出口：任何模块需要知道"东西存哪"，
都必须从这里拿，禁止在其他地方自行拼接 ~/.zxedu。
全程使用 pathlib，不硬编码 '/' 分隔符，兼容 POSIX / Windows / macOS。
"""

from __future__ import annotations

import os
from pathlib import Path


def get_zxedu_home() -> Path:
    """返回 ZXEDU 主目录。

    环境变量ZXEDU_HOME优先，未设置则用~/.zxedu。
    """
    env_home = os.environ.get("ZXEDU_HOME")
    if env_home:
        # 用户可能设 ~/xxx 或相对路径：expanduser 展开 ~，resolve 转绝对路径并解符号链接
        return Path(env_home).expanduser().resolve()
    return Path.home() / ".zxedu"


def ensure_dirs(zxedu_home: Path) -> tuple[Path, Path]:
    """创建运行期需要的目录（幂等，重复调用不报错）。

    返回（config_dir, logs_dir）。
    """
    logs_dir = zxedu_home / "logs"
    # parents=True 递归建父目录；exist_ok=True 已存在不抛异常 → 服务可随意重启
    logs_dir.mkdir(parents=True, exist_ok=True)
    return zxedu_home, logs_dir


def get_config_path() -> Path:
    """返回配置文件路径（config.yaml，现行格式）。

    经由 get_zxedu_home() 服从 ZXEDU_HOME 环境变量。
    """
    return get_zxedu_home() / "config.yaml"


def get_legacy_config_path() -> Path:
    """返回旧版配置文件路径（config.json）。

    仅在 JSON -> YAML 迁移时使用。
    """
    return get_zxedu_home() / "config.json"


def get_config_backup_path() -> Path:
    """返回配置备份路径（config.json.bak）。

    迁移成功写回之前创建。
    """
    return get_zxedu_home() / "config.json.bak"