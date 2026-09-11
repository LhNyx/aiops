"""FastAPI 服务工厂 + 自动发现的模块系统。"""

from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from fastapi import Depends, FastAPI
from starlette.responses import Response
from starlette.staticfiles import StaticFiles as _StaticFiles
from starlette.types import Scope

from zxedu_aiops import __version__
from zxedu_aiops.auth import init_hmac_key, shell_auth
from zxedu_aiops.config import load_config, save_config
from zxedu_aiops.config_models import AppConfig
from zxedu_aiops.paths import get_config_path


class StaticFiles(_StaticFiles):
    """带 Cache-Control 头的 StaticFiles。

    - *.html：no-cache（始终向服务端校验，改版后浏览器不会用旧缓存）
    - 其他（js/css/图片，通常带 hash 文件名）：max-age=86400（放心缓存一天）
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        resp = await super().get_response(path, scope)
        if resp.status_code != 200:
            return resp
        ctype = resp.headers.get("content-type", "")
        if "text/html" in ctype:
            resp.headers["Cache-Control"] = "no-cache"
        else:
            resp.headers["Cache-Control"] = "max-age=86400"
        return resp


def _discover_modules(config: AppConfig | None = None) -> list[dict[str, Any]]:
    """发现 zxedu_aiops.modules 下的所有插件模块（菜单视图）。

    只读 META 与 enabled 状态，不创建任何实例——给 GET /api/modules 用。
    """
    if config is None:
        config = load_config(get_config_path())
    modules_dir = Path(__file__).parent / "modules"
    discovered: list[dict[str, Any]] = []

    if not modules_dir.is_dir():
        return discovered

    for entry in sorted(modules_dir.iterdir()):
        # 两个硬条件：是目录、是 Python 包（有 __init__.py）——
        # __pycache__ 这类目录就被第一道筛掉
        if not entry.is_dir():
            continue
        if not (entry / "__init__.py").exists():
            continue
        try:
            mod = importlib.import_module(f"zxedu_aiops.modules.{entry.name}")
        except ImportError as exc:
            # 失败隔离：一个模块 import 崩了，警告 + 跳过，服务照常起
            print(f"[zxedu] WARNING: failed to import module '{entry.name}': {exc}", file=sys.stderr)
            continue

        # 契约验证（鸭子类型）
        if not hasattr(mod, "router") or not hasattr(mod, "META"):
            print(f"[zxedu] WARNING: module '{entry.name}' missing 'router' or 'META'", file=sys.stderr)
            continue

        meta: dict = mod.META
        name = meta.get("name", entry.name)
        # enabled 的裁决顺序：配置文件里模块段的 enabled > META 自带的 enabled
        enabled = config.modules.get(name, {}).get("enabled", meta.get("enabled", True))

        # mcp_gateway 的特殊性：enabled 取决于是否配置了分组
        # （休眠代码——没有 mcp_gateway 模块时永不触发，等那站再回头看）
        if name == "mcp_gateway":
            try:
                from zxedu_aiops.modules.mcp_gateway.config import load_gateway_config
                from zxedu_aiops.paths import get_zxedu_home

                gw_cfg = load_gateway_config(get_zxedu_home())
                enabled = len(gw_cfg.groups) > 0
            except Exception:
                pass  # 回退到默认 enabled 值
        discovered.append({
            "name": name,
            "title": meta.get("title", entry.name),
            "description": meta.get("description", ""),
            "icon": meta.get("icon", ""),
            "order": meta.get("order", 0),
            "enabled": enabled,
        })

    # 先按 order 再按 name 排序 → 管理页侧栏顺序稳定
    discovered.sort(key=lambda m: (m["order"], m["name"]))
    return discovered


def _collect_modules() -> tuple[
    list[tuple[str, Any, Any]],
    list[Callable[[], Awaitable[None]]],
    list[tuple[str, Any, Any, str]],
    list[tuple[str, str, Any]],
]:
    """单次遍历 import 所有模块，把挂载所需的全部材料收齐。

    一次 import 喂四个挂载阶段：

    - routers:            (name, router, meta) 三元组；
    - shutdown_callbacks: 来自 META["shutdown"]；
    - asgi_specs:         (挂载路径, 子应用, lifespan, name) 四元组，
                          子应用没有 lifespan 时该项为 None；
    - static_specs:       (挂载路径, 目录, 挂载名) 三元组。
    """
    modules_dir = Path(__file__).parent / "modules"
    routers: list[tuple[str, Any, Any]] = []
    shutdown_callbacks: list[Callable[[], Awaitable[None]]] = []
    asgi_specs: list[tuple[str, Any, Any, str]] = []
    static_specs: list[tuple[str, str, Any]] = []

    if not modules_dir.is_dir():
        return routers, shutdown_callbacks, asgi_specs, static_specs

    for entry in sorted(modules_dir.iterdir()):
        if not entry.is_dir() or not (entry / "__init__.py").exists():
            continue
        try:
            mod = importlib.import_module(f"zxedu_aiops.modules.{entry.name}")
        except ImportError:
            continue
        if not hasattr(mod, "router") or not hasattr(mod, "META"):
            continue

        name = entry.name
        routers.append((name, mod.router, mod.META))

        shutdown_cb: Any = mod.META.get("shutdown")
        if callable(shutdown_cb):
            shutdown_callbacks.append(cast(Callable[[], Coroutine[Any, Any, None]], shutdown_cb))

        # 模块自带 static/ 目录 → 自动挂到 /modules/{name}/
        static_dir = entry / "static"
        if static_dir.is_dir():
            static_specs.append((f"/modules/{name}", str(static_dir), f"module-static-{name}"))

        # 模块声明 asgi_mounts → 挂完整 ASGI 子应用（如 mcp_gateway 的 MCP 端点）。
        # 工厂每调用一次产出一个新实例（带自己的 lifespan 状态），所以只能调一次
        asgi_mounts = mod.META.get("asgi_mounts") or {}
        for mount_path, factory in asgi_mounts.items():
            sub_app = factory()
            asgi_specs.append((mount_path, sub_app, getattr(sub_app, "lifespan", None), name))

    return routers, shutdown_callbacks, asgi_specs, static_specs


def _mount_modules(
    app: FastAPI, collected: tuple | None = None
) -> tuple[list[Callable[[], Awaitable[None]]], list[Any]]:
    """把收集好的插件模块挂载到 app 上。

    collected 允许调用方传入早前 _collect_modules() 的结果，
    让 ASGI 子应用实例（及其 lifespan）被复用而不是重建。

    返回 (shutdown_callbacks, asgi_lifespans)。ASGI lifespan 被单独交还
    给调用方组合进 app 的 lifespan —— Starlette 的 Mount 不会把父应用的
    lifespan 传播给子应用，不手动组合的话，FastMCP 这类在自己的 lifespan
    里初始化 StreamableHTTPSessionManager 的子应用首个请求就会崩。
    """
    if collected is None:
        collected = _collect_modules()
    routers, shutdown_callbacks, asgi_specs, static_specs = collected

    for name, router, meta in routers:
        # 默认 auth="shell" → 注入壳层鉴权；声明 "own"/"public" 则交还模块自管
        shell_deps = [] if meta.get("auth", "shell") != "shell" else [Depends(shell_auth)]
        app.include_router(
            router,
            prefix=f"/api/modules/{name}",
            tags=[f"module:{name}"],
            dependencies=shell_deps,
        )

        # 声明 config_schema 的模块 → 框架代为生成两个配置端点
        if meta.get("config_schema"):

            @app.get(f"/api/modules/{name}/config", dependencies=shell_deps)
            async def get_module_config(_name: str = name) -> dict[str, Any]:
                # 参数默认值技巧：默认值在函数定义时求值，每次循环绑定的
                # 是"当轮"的 name；若直接闭包引用 name，十个模块会共享
                # 循环结束时最后一个 name（Python 闭包的经典陷阱）
                cfg = load_config(get_config_path())
                return cfg.modules.get(_name, {})

            @app.put(f"/api/modules/{name}/config", dependencies=shell_deps)
            async def update_module_config(body: dict[str, Any], _name: str = name) -> dict[str, Any]:
                cfg = load_config(get_config_path())
                # 浅合并：新 body 覆盖同名字段，模块其余配置原样保留
                cfg.modules[_name] = {**cfg.modules.get(_name, {}), **body}
                save_config(get_config_path(), cfg)
                return cfg.modules[_name]

    for mount_path, directory, mount_name in static_specs:
        app.mount(mount_path, StaticFiles(directory=directory, html=True), name=mount_name)

    asgi_lifespans: list[Any] = []
    for mount_path, sub_app, lifespan, name in asgi_specs:
        app.mount(mount_path, sub_app, name=f"module-asgi-{name}")
        if lifespan is not None:
            asgi_lifespans.append(lifespan)

    return shutdown_callbacks, asgi_lifespans


def _mask_secret(secret: str) -> str:
    """脱敏 api_key：前 4 + 后 3，长度 ≤ 8 时全 ``*``。"""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}***{secret[-3:]}"


def create_app(config: AppConfig | None = None) -> FastAPI:
    """创建并配置 FastAPI 应用（应用工厂）。"""
    init_hmac_key()

    if config is None:
        config = load_config(get_config_path())

    # 模块只收集一次：同一批 ASGI 子应用实例既喂 lifespan 组合（下面）
    # 又喂实际挂载。工厂函数每次调用产出带全新 lifespan 状态的新实例，
    # 收集两次的话，第二批实例的 lifespan 永远不会被执行
    collected = _collect_modules()
    asgi_specs = collected[2]
    asgi_lifespans = [ls for _, _, ls, _ in asgi_specs if ls is not None]

    # holder 打破定义顺序的循环：lifespan 函数现在定义，shutdown 回调
    # 要到 _mount_modules 跑完才有——用可变 dict 装着，闭包引用它即可
    holder: dict[str, Any] = {"shutdown": []}

    @asynccontextmanager
    async def base_lifespan(_app: FastAPI):
        yield
        # shutdown 段 —— uvicorn 收到 SIGTERM/SIGINT 后、退出前会走到这里
        # （正好接上第 4 站 terminate_process 的"优雅终止"），模块资源
        # （如 SSH ControlMaster socket）得以确定性清理，而不是孤儿化丢给 PID 1
        for cb in holder["shutdown"]:
            try:
                await cb()
            except Exception:
                print("[zxedu] WARNING: error during module shutdown", file=sys.stderr)

    if asgi_lifespans:
        # 延迟导入：fastmcp 是重依赖，只在真的有 ASGI 子应用时才加载
        # （当前没有任何模块声明 asgi_mounts，这段休眠）
        from fastmcp.utilities.lifespan import combine_lifespans

        lifespan = combine_lifespans(base_lifespan, *asgi_lifespans)
    else:
        lifespan = base_lifespan

    app = FastAPI(
        title="ZXEdu AIOps",
        version=__version__,
        # 关闭 Swagger/ReDoc 文档页：这是对外暴露的运维服务，
        # /docs 就是全部管理接口的"攻击面地图"，没理由主动发出去
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # 健康端点 —— process.py 的 health_check 探测的目标
    @app.get("/api/health")
    async def health():
        return {"status": "healthy", "version": __version__}

    # 模块清单 —— 管理页侧栏菜单的数据源
    @app.get("/api/modules", dependencies=[Depends(shell_auth)])
    async def list_modules():
        return {"modules": _discover_modules()}

    # 壳层配置端点
    @app.get("/api/config", dependencies=[Depends(shell_auth)])
    async def get_server_config():
        cfg = load_config(get_config_path())
        return cfg.model_dump()

    @app.put("/api/config", dependencies=[Depends(shell_auth)])
    async def update_server_config(body: dict[str, Any]):
        cfg = load_config(get_config_path())
        if "server" in body:
            for key, value in body["server"].items():
                # hasattr 白名单：只接受 ServerConfig 上已存在的字段，
                # 未知字段静默丢弃（前向兼容 + 防任意字段注入）
                if hasattr(cfg.server, key):
                    setattr(cfg.server, key, value)
        save_config(get_config_path(), cfg)
        return cfg.model_dump()

    # 挂载插件模块
    holder["shutdown"], _asgi_lifespans = _mount_modules(app, collected)

    # 管理壳 SPA —— 必须最后挂：Starlette 按注册顺序匹配路由，
    # "/" 是兜底 catch-all，先挂它会吞掉所有 API 路由
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app