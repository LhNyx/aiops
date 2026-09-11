"""hello —— 最小插件模块，用于验证模块系统的自动发现与挂载。"""

from datetime import datetime

from fastapi import APIRouter

router = APIRouter()


@router.get("/time")
async def get_time() -> dict[str, str]:
    """返回服务器当前时间。"""
    return {"now": datetime.now().isoformat()}


META = {
    "name": "hello",
    "title": "Hello",
    "description": "最小示例模块：验证自动发现与挂载",
    "icon": "Clock",
    "order": 99,
}