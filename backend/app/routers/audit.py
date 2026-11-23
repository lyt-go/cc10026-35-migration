"""调用审计的区间查询、归档预览、分片迁移与边界样本接口。

路由必须注册在通用 `/{module}/{entry_id}` 之前，否则 `audit` 下的固定路径
会被动态路径抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.schemas import ActionResult
from app.services import audit_store, service_for

router = APIRouter(prefix="/api/audit", tags=["调用审计"])


def _audit_service():
    return service_for("call_audit")


@router.get("/records")
def query_records(
    start_time: str | None = Query(default=None),
    end_time: str | None = Query(default=None),
    api_name: str | None = Query(default=None),
    caller: str | None = Query(default=None),
    end_exclusive: bool = Query(default=False),
) -> dict[str, Any]:
    """跨月区间查询：旧目录 ∪ 月分片，原编号去重，附对账信息。"""
    return _audit_service().query_range(
        start_time=start_time, end_time=end_time, api_name=api_name,
        caller=caller, end_exclusive=end_exclusive)


@router.get("/reconcile")
def reconcile(
    start_time: str | None = Query(default=None),
    end_time: str | None = Query(default=None),
    api_name: str | None = Query(default=None),
    caller: str | None = Query(default=None),
    end_exclusive: bool = Query(default=False),
) -> dict[str, Any]:
    """只返回对账摘要：来源计数、去重量、分片完整性。"""
    records, reconciliation = audit_store.query_audit(
        api_name=api_name, caller=caller, start_time=start_time,
        end_time=end_time, end_exclusive=end_exclusive)
    return {"reconciliation": reconciliation, "count": len(records)}


@router.get("/shards")
def shards() -> dict[str, Any]:
    """按月分片清单与迁移状态。"""
    return audit_store.shard_overview()


@router.get("/archive/preview")
def archive_preview(before_date: str = Query(...)) -> dict[str, Any]:
    """归档预览：按原日期切历史明细，核对跨月归属。"""
    return _audit_service().archive_preview(before_date)


@router.post("/archive")
def archive(before_date: str = Query(...)) -> dict[str, Any]:
    """执行归档：同时覆盖旧目录和月分片，保留原编号与实际写入月份。"""
    result = _audit_service().archive_old_records(before_date)
    return {
        "ok": True,
        "archive_file": result.get("archive_file"),
        "archived_count": result.get("archived_count"),
        "month_counts": result.get("month_counts"),
        "source_counts": result.get("source_counts"),
        "duplicates_removed": result.get("duplicates_removed"),
        "reconciliation": result.get("reconciliation"),
    }


@router.post("/migrate")
def migrate(
    months: str | None = Query(default=None, description="逗号分隔的月份，如 2026-10,2026-11"),
    per_month_limit: int | None = Query(default=None, ge=1),
    remove_legacy: bool = Query(default=False),
) -> dict[str, Any]:
    """把旧目录逐条 JSON 迁入月分片；可重复执行，不会重复写入。"""
    wanted = [m.strip() for m in months.split(",") if m.strip()] if months else None
    return _audit_service().migrate_legacy(
        wanted, per_month_limit=per_month_limit, remove_legacy=remove_legacy)


@router.post("/samples", response_model=ActionResult)
def seed_samples() -> ActionResult:
    """写入三个边界样本，并在查看/归档两个入口返回核对结果。"""
    result = _audit_service().boundary_samples(seed=True)
    return ActionResult(ok=True, message="边界样本已就绪", entry=result)
