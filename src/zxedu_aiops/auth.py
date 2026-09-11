"""统一的 admin API 鉴权依赖。

所有受保护的管理接口（壳层 + 模块）共用 shell_auth。鉴权规则（按顺序）：
- localhost（127.0.0.1 / ::1 / testclient）无条件放行
- 携带有效 HMAC 签名参数的临时链接（未过期、签名匹配）放行
- 远程请求必须携带 Authorization: Bearer <admin_api_key>，否则 401
- ZXEDU_MCP_FORCE_AUTH=1 环境变量强制开启鉴权（覆盖 localhost 旁路，测试用）

模块通过 META["auth"] 声明鉴权策略：
- "shell"（默认）→ 框架在 include_router 时自动注入 shell_auth
- "own"         → 模块自管鉴权（所有受保护端点显式 Depends(shell_auth)）
- "public"      → 模块有公开端点，按端点粒度声明 Depends(shell_auth)

HMAC 临时链接
--------------
generate_temp_link(path, ttl_seconds) 为指定路径生成带 HMAC 签名的临时访问
链接，在 TTL 内有效，通过 query string 携带签名参数，无需 Bearer token。

签名载荷：{path}|{expires}|{nonce}。nonce 仅作为盐值确保每次链接唯一，
不做服务端追踪（防重放靠过期时间）。
"""

from __future__ import annotations

import hmac
import os
import secrets
import time

from fastapi import HTTPException, Request

from zxedu_aiops.config import load_config
from zxedu_aiops.paths import get_config_path

# "testclient" 是 FastAPI TestClient 发请求时 client.host 的值——
# 把测试环境也当成可信来源，测试用例无需配 key（第 9 站会用到）
TRUSTED_CLIENTS = frozenset({"127.0.0.1", "::1", "testclient"})

# HMAC 签名密钥 —— 启动时内存中生成，不落盘。
# 重启服务 = 密钥换新 = 所有在途临时链接全部失效（有意为之的安全属性）
_hmac_key: str | None = None


def init_hmac_key() -> None:
    """初始化 HMAC 签名密钥（服务启动时调用一次）。

    生成 32 字节随机 hex 存入模块级 _hmac_key。
    """
    global _hmac_key
    _hmac_key = secrets.token_hex(32)


def generate_temp_link(path: str, ttl_seconds: int = 300) -> str:
    """为指定 API 路径生成带 HMAC 签名的临时链接 query string。

    Args:
        path: API 路径，如 /api/modules/skills/my-skill/download。
        ttl_seconds: 有效期（秒），默认 300（5 分钟）。

    Returns:
        query string 部分，形如 ?expires=...&nonce=...&sig=...。
        调用方自行拼接 base_url。

    Raises:
        RuntimeError: HMAC 密钥未初始化（需先调用 init_hmac_key()）。
    """
    if _hmac_key is None:
        raise RuntimeError("HMAC key not initialized — call init_hmac_key() at startup")

    expires = int(time.time()) + ttl_seconds
    nonce = secrets.token_hex(16) # 一次随机数
    payload = f"{path} | {expires} | {nonce}"
    sig = hmac.new(_hmac_key.encode(), payload.encode(), "sha256").hexdigest()

    return f"?expires={expires}&nonce={nonce}&sig={sig}"


async def shell_auth(request: Request) -> None:
    """管理接口鉴权依赖：localhost 放行，HMAC 临时链接放行，远程必须 Bearer admin_api_key。"""
    # request.client.host 来自 TCP 层（握手时的对端地址），不是 HTTP header——
    # 伪造 X-Forwarded-For 骗不过它。反代场景下这里看到的是代理 IP，
    # 天然不被信任，鉴权照常生效
    client_ip = request.client.host if request.client else None
    if client_ip in TRUSTED_CLIENTS and not os.environ.get("ZXEDU_MCP_FORCE_AUTH"):
        return

    # 第二道：HMAC 临时链接（有效期 + 签名双验证）
    if _hmac_key is not None:
        params = request.query_params
        sig = params.get("sig")
        expires_raw = params.get("expires")
        nonce = params.get("nonce")
        if sig and expires_raw and nonce:
            try:
                expires = int(expires_raw)
            except ValueError:
                raise HTTPException(status_code=401, detail="invalid expires parameter") from None
            if expires > int(time.time()):
                payload = f"{request.url.path}|{expires}|{nonce}"
                expected = hmac.new(
                    _hmac_key.encode(), payload.encode(), "sha256"
                ).hexdigest()
                # compare_digest 常数时间比较——逐字节对比的提前返回时间差
                # 会泄露签名前缀（时序攻击），这个函数杜绝这条侧信道
                if hmac.compare_digest(sig, expected):
                    return
            raise HTTPException(status_code=401, detail="invalid or expired temp link")

    # 第三道：Bearer admin_api_key
    # 每个请求都重新读盘——PUT /api/config 改了 key 下一秒生效，
    # 代价是每次一个几百字节 yaml 的读，管理端点频率下完全可接受
    cfg = load_config(get_config_path())
    key = cfg.server.admin_api_key
    if not key:
        raise HTTPException(status_code=401, detail="admin_api_key not configured")
    if request.headers.get("authorization", "") != f"Bearer {key}":
        raise HTTPException(status_code=401, detail="invalid admin api key")