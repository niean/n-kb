"""只读自检 API（T9）：GET /observations/otel。

路由只调用组合根注入 ``app.state.observation_snapshot`` 的纯函数快照
callable（合同：``snapshot(page, size) -> dict``），不取得 runtime、
Infrastructure 或任何 SDK；响应禁用缓存，仅输出安全投影（无完整配置、
凭据、原异常），不发起任何网络探测。

分页约束：``page`` 取 1..10（ring buffer 最近 100 条已结束 sampled span
按 size=10 至多 10 页）；``size`` 固定 10（保留参数但只接受 10）。非法/
负数/零/超界一律 422。
"""
from fastapi import APIRouter, Query, Request, Response

router = APIRouter()

OBSERVATIONS_PAGE_MAX = 10
OBSERVATIONS_PAGE_SIZE = 10


@router.get("/observations/otel")
def observations_otel(
    request: Request,
    response: Response,
    page: int = Query(default=1, ge=1, le=OBSERVATIONS_PAGE_MAX),
    size: int = Query(
        default=OBSERVATIONS_PAGE_SIZE,
        ge=OBSERVATIONS_PAGE_SIZE,
        le=OBSERVATIONS_PAGE_SIZE,
    ),
) -> dict:
    snapshot = request.app.state.observation_snapshot(page=page, size=size)
    response.headers["Cache-Control"] = "no-store"
    return snapshot
