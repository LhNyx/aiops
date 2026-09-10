"""服务配置：Pydantic v2 模型 + YAML 持久化。

替代早期基于 dataclass 的 JSON 配置；首次加载时自动把旧的
config.json 迁移为 config.yaml。

公共 API
--------
load_config(path)   → AppConfig | ServerConfig （自动识别格式）
save_config(path, config)                       （两种类型都接受）
ServerConfig       → 向后兼容的扁平包装层（已废弃）
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic.v1.utils import sequence_like
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from zxedu_aiops.config_models import AppConfig
from zxedu_aiops.paths import (
    get_config_backup_path,
    get_config_path,
    get_legacy_config_path,
)

logger = logging.getLogger(__name__)

# 往返模式（round-trip）YAML —— 读写保留注释、顺序、格式
_yaml = YAML(typ="rt")
_yaml.indent(mapping=2, sequence=4, offset=2)

# ──────────────────────────────────────────────────────────────────────
#  扁平 ↔ 嵌套 映射表（ServerConfig 包装层和 JSON 迁移共用）
# ──────────────────────────────────────────────────────────────────────

# 第1代 JSON 的全部已知扁平 key（迁移时用来区分"已知字段"和"未知保留字段"）
_KNOWN_FLAT_KEYS: frozenset[str] = frozenset({
    "host",
    "port",
    "log_level",
    "deepseek_api_key",
    "jumpserver_enabled",
    "jumpserver_base_url",
    "jumpserver_api_key_id",
    "jumpserver_api_key_secret",
    "jumpserver_tls_cert_path",
    "jumpserver_tls_key_path",
    "jumpserver_tls_ca_cert_path",
    "jumpserver_tls_key_password",
    "jumpserver_tls_verify",
})

# (属性链，默认值) -- 属性链是 str/int 的元组，可迭代喂给
# getattr / __getitem__，一层层走进嵌套结构。
_FLAT_GETTERS: dict[str, tuple[tuple[str | int, ...], Any]] = {
    "host":                     (("server", "host"), "127.0.0.1"),
    "port":                     (("server", "port"), 55002),
    "log_level":                (("server", "log_level"), "info"),
    "deepseek_api_key":         (("server", "deepseek_api_key"), ""),
    "jumpserver_enabled":       (("modules", "jumpserver", "enabled"), False),
    "jumpserver_base_url":      (("modules", "jumpserver", "endpoints", 0, "base_url"),"https://monitor.i-school.net"),
    "jumpserver_api_key_id":    (("modules", "jumpserver", "endpoints", 0, "api_key_id"), ""),
    "jumpserver_api_key_secret": (("modules", "jumpserver", "endpoints", 0, "api_key_secret"), ""),
    "jumpserver_tls_cert_path": (("modules", "jumpserver", "tls", "cert_path"), ""),
    "jumpserver_tls_key_path":  (("modules", "jumpserver", "tls", "key_path"), ""),
    "jumpserver_tls_ca_cert_path": (("modules", "jumpserver", "tls", "ca_cert_path"), ""),
    "jumpserver_tls_key_password": (("modules", "jumpserver", "tls", "key_password"), ""),
    "jumpserver_tls_verify":    (("modules", "jumpserver", "endpoints", 0, "verify"), True),
}


def _nested_get(obj: object, path: tuple[str | int, ...], default: object) -> object:
    """沿 path 逐层深入obj；任何一步失败都返回 default。"""
    for key in path:
        try:
            if isinstance(obj, dict):
                obj = obj[key]
            elif isinstance(obj, (list, tuple)):
                obj = obj[int(key)]
            else:
                obj = getattr(obj, str(key))
        except (KeyError, AttributeError, IndexError, ValueError, TypeError):
            return default
    return obj


def _nested_set(obj: object, path: tuple[str | int, ...], value: object) -> None:
    """沿 path 逐层深入obj，在最后一层写入value（中间层缺失则现场创建）"""
    head = list(path)
    for key in head[:-1]:
        if isinstance(obj, dict):
            obj = obj.setdefault(key, {})
        elif isinstance(obj, list):
            idx = int(key)
            while len(obj) <= idx: # 列表不够长就补空 dict 到位
                obj.append({})
            obj = obj[idx]
        else:
            sk = str(key)
            if not hasattr(obj, sk):
                setattr(obj, sk, {})
            obj = getattr(obj, sk)
    last = head[-1]
    if isinstance(obj, dict):
        obj[last] = value
    elif isinstance(obj, list):
        obj[int(last)] = value
    else:
        setattr(obj, str(last), value)


# ──────────────────────────────────────────────────────────────────────
#  ServerConfig —— 向后兼容的扁平包装层
# ──────────────────────────────────────────────────────────────────────

class ServerConfig:
    """旧版扁平配置包装层 —— 委托给 AppConfig。

    **已废弃**，新代码应直接用 AppConfig。
    存在的意义：让 ``from zxedu_aiops.config import ServerConfig``
    在 JSON→YAML 迁移期继续可用。

    对全部 13 个旧扁平字段名（host / port / log_level / deepseek_api_key /
    jumpserver_*）支持 hasattr / getattr / setattr。
    """

    def __init__(self, app_config: AppConfig | None = None, **kwargs: Any) -> None:
        # 绕过自定义 __setattr__，把真实载体存在实例属性里
        object.__setattr__(self, "_app", app_config if app_config is not None else AppConfig())
        object.__setattr__(self, "_extra", {})
        # 兼容旧 dataclass 构造器的扁平关键字参数
        for field_name, value in kwargs.items():
            if field_name in _FLAT_GETTERS:
                path, _ = _FLAT_GETTERS[field_name]
                _nested_set(self._app, path, value)
            else:
                self._extra[field_name] = value

    # -- 属性访问 -----------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        if name in _FLAT_GETTERS:
            path, default = _FLAT_GETTERS[name]
            return _nested_get(self._app, path, default)
        extra: dict[str, Any] = object.__getattribute__(self, "_extra")
        if name in extra:
            return extra[name]
        raise AttributeError(f"'ServerConfig' object has no attribute '{name}'")

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("_app", "_extra"):  # 内部载体直接落实例字典，防止无限递归
            object.__setattr__(self, name, value)
            return
        if name in _FLAT_GETTERS:
            path, _ = _FLAT_GETTERS[name]
            _nested_set(self._app, path, value)
            return
        extra: dict[str, Any] = object.__getattribute__(self, "_extra")
        extra[name] = value

    # -- 工厂方法 -----------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServerConfig:
        """从扁平 dict（旧 JSON 格式）创建。"""
        inst = cls()
        for field_name, (_, default) in _FLAT_GETTERS.items():
            setattr(inst, field_name, data.get(field_name, default))
        inst._extra = {k: v for k, v in data.items() if k not in _KNOWN_FLAT_KEYS}
        return inst

    def to_dict(self) -> dict[str, Any]:
        """序列化为扁平 dict（旧 JSON 格式）。"""
        result: dict[str, Any] = {}
        for field_name, (path, default) in _FLAT_GETTERS.items():
            result[field_name] = _nested_get(self._app, path, default)
        result.update(self._extra)
        return result

    # -- 类型转换 -----------------------------------------------------------

    def to_app_config(self) -> AppConfig:
        """返回底层 AppConfig。"""
        return self._app

    @classmethod
    def from_app_config(cls, app: AppConfig) -> ServerConfig:
        """包装一个现成的 AppConfig。"""
        return cls(app_config=app)

# ──────────────────────────────────────────────────────────────────────
#  公共 API
# ──────────────────────────────────────────────────────────────────────

def load_config(config_path: Path | None = None) -> AppConfig | ServerConfig:
    """加载应用配置，必要时自动从旧 JSON 迁移。

    返回类型取决于路径：
    - 传 .json 路径     → 返回 ServerConfig（扁平包装层，向后兼容）
    - 传 .yaml 或 None → 返回 AppConfig

    逻辑三分支：
    1. config.yaml 存在   → 加载 + 校验；
    2. 只有 config.json    → 先迁移再加载；
    3. 两个都不存在        → 生成默认值 → 落盘 → 返回。
    """
    want_flat = config_path is not None and config_path.suffix == ".json"

    if config_path is not None and config_path.suffix == ".json":
        yaml_path = config_path.with_suffix(".yaml")
        legacy_path = config_path
    else:
        yaml_path = _resolve_yaml_path(config_path)
        legacy_path = get_legacy_config_path()

    # 1. YAML 在 —— 直接读
    if yaml_path.exists():
        app = _load_yaml(yaml_path)
        return ServerConfig.from_app_config(app) if want_flat else app

    # 2. 只有 JSON 在 —— 迁移
    if legacy_path.exists():
        _migrate_json_to_yaml(legacy_path, yaml_path)
        app = _load_yaml(yaml_path)
        return ServerConfig.from_app_config(app) if want_flat else app

    # 3. 都不在 —— bootstrap 默认值
    app = AppConfig()
    # 注入默认 JumpServer 端点（默认禁用）——让用户打开文件就能看到
    # 完整结构、知道往哪填密钥，而不是面对一个空 modules 段瞎猜
    app.modules["jumpserver"] = {
        "enabled": False,
        "endpoints": [
            {
                "name": "爱上学生产",
                "base_url": "https://monitor.i-school.net",
                "api_key_id": "",
                "api_key-secret": "",
                "verify": True,
            }
        ],
        "tls": {
            "cert_path": "",
            "key_path": "",
            "ca_cert_path": "",
            "key_password": "",
        },
        "dbeaver_path": "",
        "db_client": "",
    }
    save_config(yaml_path, app)
    return ServerConfig.from_app_config(app) if want_flat else app


def save_config(config_path: Path, app_config: AppConfig | ServerConfig) -> None:
    """把配置持久化为 YAML（往返模式，保留注释）。

    同时接受 AppConfig 和旧版 ServerConfig 包装层。
    若 config_path 以 .json 结尾，写入会被静默重定向到 .yaml
    （JSON→YAML 迁移是单行道，没有回头路）。
    """
    if isinstance(app_config, ServerConfig):
        app_config = app_config.to_app_config()

    if config_path.suffix == ".json":
        config_path = config_path.with_suffix(".yaml")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = app_config.model_dump(exclude_none=False, mode="python")
    with open(config_path, "w", encoding="utf-8") as fh:
        _yaml.dump(data, fh)


# ──────────────────────────────────────────────────────────────────────
#  内部辅助
# ──────────────────────────────────────────────────────────────────────


def _resolve_yaml_path(config_path: Path | None) -> Path:
    """把用户传入的路径归一为 YAML 路径。"""
    if config_path is None:
        return get_config_path()
    if config_path.suffix in (".yaml", ".yml"):
        return config_path
    return get_config_path()


def _load_yaml(yaml_path: Path) -> AppConfig:
    """加载并校验 YAML 配置文件。

    抛出
    ----
    ValueError
        YAML 语法非法时（错误信息带行号）。
    """
    try:
        with open(yaml_path, encoding="utf-8") as fh:
            data = _yaml.load(fh)
    except YAMLError as exc:
        # ruamel 的异常带 problem_mark（行列坐标），转成用户能看懂的报错
        line_info = ""
        if hasattr(exc, "problem_mark") and exc.problem_mark is not None:
            pm = exc.problem_mark
            line_info = f"(line{pm.line + 1}, column {pm.column + 1})"
        raise ValueError(
            f"Invalid YAML in {yaml_path}{line_info}: {exc}"
        ) from exc
    except FileNotFoundError:
        data = {}

    if not isinstance(data, dict):
        data = {}

    return AppConfig.model_validate(data)

def _migrate_json_to_yaml(json_path: Path, yaml_path: Path) -> None:
    """把旧 config.json 转换为 config.yaml。

    幂等 —— 只有 YAML 成功写盘**之后**才把 JSON 改名为 .json.bak，
    中途崩溃下次重来也不会丢数据。备份已存在则跳过改名。
    损坏或空的 JSON 文件优雅回退到默认值。
    """
    # 1. 解析 JSON（损坏时优雅回退）
    try:
        raw = json_path.read_text(encoding="utf-8").strip()
        flat: dict[str, Any] = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, FileNotFoundError) as exc:
        logger.warning(
            "Cannot parse legacy config %s, using defaults: %s",
            json_path,
            exc,
        )
        flat = {}

    if not isinstance(flat, dict):
        flat = {}

    # 2. 扁平 key → 嵌套 YAML 结构（硬编码的翻译表）
    yaml_data: dict[str, Any] = {
        "server": {
            "host": flat.get("host", "127.0.0.1"),
            "port": int(flat.get("port", 55002)),
            "log_level": flat.get("log_level", "info"),
            "deepseek_api_key": flat.get("deepseek_api_key", ""),
        },
        "modules": {
            "jumpserver": {
                "enabled": flat.get("jumpserver_enabled", False),
                "endpoints": [
                    {
                        "name": "爱上学生产",
                        "base_url": flat.get(
                            "jumpserver_base_url",
                            "https://monitor.i-school.net",
                        ),
                        "api_key_id": flat.get("jumpserver_api_key_id", ""),
                        "api_key_secret": flat.get("jumpserver_api_key_secret", ""),
                        "verify": flat.get("jumpserver_tls_verify", True),
                    }
                ],
                "tls": {
                    "cert_path": flat.get("jumpserver_tls_cert_path", ""),
                    "key_path": flat.get("jumpserver_tls_key_path", ""),
                    "ca_cert_path": flat.get("jumpserver_tls_ca_cert_path", ""),
                    "key_password": flat.get("jumpserver_tls_key_password", ""),
                },
            }
        },
    }

    # 未知顶层 key 原样保留（前向兼容，和 extra=allow 一个思路）
    for k, v in flat.items():
        if k not in _KNOWN_FLAT_KEYS:
            yaml_data[k] = v

    # 3. 校验
    app = AppConfig.model_validate(yaml_data)

    # 4. 写 YAML
    save_config(yaml_path, app)

    # 5. JSON 改名 → .json.bak（仅在成功后，幂等）
    backup_path = get_config_backup_path()
    if backup_path.exists():
        logger.info("Backup already exists at %s, skipping rename", backup_path)
    else:
        try:
            json_path.rename(backup_path)
        except OSError as exc:
            logger.warning(
                "Cannot rename %s → %s: %s", json_path, backup_path, exc
            )

    logger.info(
        "Migrated %s → %s (backup: %s)",
        json_path,
        yaml_path,
        backup_path,
    )