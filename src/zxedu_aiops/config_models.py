"""多段式 YAML 配置的 Pydantic v2 模型。

与 config.yaml 的结构一一对应：
  server:   ServerConfig — host、port、log_level、llm_provider、api_keys
  modules:
    jumpserver: JumpserverConfig — enabled、endpoints[]、tls
    <其他模块>:  dict[str, Any]   — 各模块自便的灵活配置

provider 的 base_url/models 内置在 zxedu_aiops.llm 中，配置文件只保留
server.llm_provider 选择、server.llm_model 模型名和对应的 api_key。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class TLSConfig(BaseModel):
    """TLS 客户端证书配置（JumpsServer 双向认证可用）。"""

    model_config = {"extra": "allow"}

    cert_path: str = ""
    key_path: str = ""
    ca_cert_path: str = ""
    key_password: str = ""


class EndpointConfig(BaseModel):
    """单个 JumpServer 接入端点。"""

    model_config = {"extra": "allow"}

    name: str
    base_url: str
    api_key_id: str = ""
    verify: bool = True


class JumpserverConfig(BaseModel):
    """模块级 JumpServer 配置。"""

    model_config = {"extra": "allow"}

    enabled: bool = False
    endpoints: list[EndpointConfig] = []
    tls: TLSConfig = TLSConfig()
    dbeaver_path: str = "" # 空=自动检测；非空=自定义 DBeaver 路径
    db_client: str = "" # 空=自动 fallback；"dbeaver"=强制 DBeaver；"mysql_cli"=强制 mysql cli


class ServerConfig(BaseModel):
    """服务运行配置（server 段）。"""

    model_config = {"extra": "allow"}

    host: str = "127.0.0.1"
    port: int = 55002
    log_level: str = "info"
    # ── LLM provider 选择 ──────────────────────────────────────────
    # 固定二选一：deepseek / zxedu。provider 的 base_url/models 内置在
    # zxedu_aiops.llm，这里只选 provider、模型名和对应 api_key。
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-v4-flash" # 全局模型名（纯名，无provider前缀）
    deepseek_api_key: str = ""
    zxedu_api_key: str = ""
    admin_api_key: str = ""
    gitlab_personal_token: str = ""
    cdn_base_url: str = "https://dev-cd.i-school.net:60443/imageng/demo/nashira/aiops"

    @property
    def cli_host(self) -> str:
        """CLI 工具用来连接的主机名。

        服务绑到 0.0.0.0（或 ::）时监听所有网卡，但客户端没法
        "连接到 0.0.0.0"——这个属性把通配绑定地址解析成 127.0.0.1，
        供本机 CLI 使用。
        """
        if self.host in ("0.0.0.0", "::"):
            return "127.0.0.1"
        return self.host

    @property
    def cli_base_url(self) -> str:
        """CLI 访问本地服务的基础URL。"""
        return f"http://{self.cli_host}:{self.port}"


class AppConfig(BaseModel):
    """顶层应用配置 --- 对应整个 config.yaml。"""

    model_config = {"extra": "allow"}

    server: ServerConfig = ServerConfig()
    modules: dict[str, Any] = {}