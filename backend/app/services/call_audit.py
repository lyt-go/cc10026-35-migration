"""调用审计领域服务：管理API调用的审计记录、归档和查询追溯。

存储已迁移到按月分片的审计数据（data/audit/monthly/），同时兼容迁移前的
逐条 JSON（data/audit/*.json）。两套来源并存时按原始编号去重，同一次请求
只算一笔；分片未迁移完也不会让旧目录里已存在的历史行消失。

跨模块数据流：
- 消费 usage_billing 快照 → 用量异常触发审计核查
- 消费 subscription 共享记录 → 审批通过的订阅记录审计
- 消费 export_task 快照 → 导出操作记录审计
- 审计记录定期归档 → 写入 data/audit/archive/ 共享目录
- 被 error_classify 消费 → 异常调用触发错误归类
"""
from __future__ import annotations

from typing import Any

from app.services import audit_store
from app.services.base import BaseModuleService
from app.store import store


def _row_from_record(record: dict[str, Any], fallback_id: int | None = None) -> dict[str, Any]:
    """把审计明细映射成前端列表行，附带来源与月份归属。"""
    origin_no = record.get("_origin_record_no") or audit_store.record_no(record)
    origin_id = record.get("_origin_audit_id")
    if origin_id is None:
        origin_id = audit_store.origin_audit_id(record)
    row_id = origin_id if isinstance(origin_id, int) else fallback_id
    if row_id is None:
        try:
            row_id = int(str(origin_id))
        except (TypeError, ValueError):
            row_id = fallback_id if fallback_id is not None else 0
    logged_month = record.get("_logged_month")
    if not logged_month:
        logged_month = audit_store.month_of(
            record.get("_logged_at") or record.get("timestamp") or record.get("时间戳"))
    return {
        "id": row_id,
        "记录编号": origin_no or record.get("记录编号"),
        "调用方": record.get("caller") or record.get("调用方"),
        "目标API": record.get("api_name") or record.get("目标API"),
        "请求参数": record.get("request_params") or record.get("请求参数"),
        "响应码": record.get("response_code") or record.get("响应码"),
        "响应耗时": record.get("latency_ms") if record.get("latency_ms") is not None
        else record.get("响应耗时"),
        "返回行数": record.get("result_rows") if record.get("result_rows") is not None
        else record.get("返回行数"),
        "时间戳": record.get("timestamp") or record.get("时间戳") or record.get("_logged_at"),
        "status": record.get("status") or "已记录",
        "pending": record.get("status") not in ("已归档", "已过期"),
        "abnormal": record.get("abnormal", False),
        # 归属信息：原始编号、来源、归属月份、新数据实际写入的月份
        "原始编号": origin_no,
        "数据来源": record.get("_source") or "内存样例",
        "归属月份": logged_month,
        "写入月份": record.get("_written_month"),
        "原始时间戳": record.get("timestamp") or record.get("时间戳"),
        "时区": record.get("_timezone") or audit_store.TZ_LABEL,
    }


class CallAuditService(BaseModuleService):
    MODULE = "call_audit"
    ENTITY = "调用记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "调用方", "目标API", "请求参数", "响应码", "响应耗时", "返回行数", "时间戳"]
    REQUIRED_FIELDS = ["记录编号", "调用方", "目标API"]
    STATUS_ORDER = ["已记录", "已校验", "已归档", "已过期"]
    ACTION_RULES = {"确认校验": "已校验", "提交归档": "已归档", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = []

    @property
    def export_fields(self) -> list[str]:
        # 导出带上归属信息，便于和归档结果按原编号对账。
        return [*self.LIST_FIELDS, "原始编号", "归属月份", "写入月份", "数据来源"]

    # ---- 写入 ----

    def record_api_call(self, caller: str, api_name: str, params: str = "",
                        response_code: str = "200", latency_ms: float = 0.0,
                        result_rows: int = 0, timestamp: str | None = None) -> dict[str, Any]:
        """记录一次API调用到按月分片的审计数据。"""
        from app.services.data_manager import write_audit

        audit_rows = store.rows(self.MODULE)
        memory_max = max((int(r.get("id", 0)) for r in audit_rows), default=0)
        new_id = max(memory_max, audit_store.next_audit_id() - 1) + 1
        record_no = f"CALL-{new_id:04d}"
        logged_at = timestamp or self._now_iso()

        audit_data = {
            "audit_id": new_id,
            "record_no": record_no,
            "记录编号": record_no,
            "caller": caller,
            "api_name": api_name,
            "request_params": params,
            "response_code": response_code,
            "latency_ms": latency_ms,
            "result_rows": result_rows,
            "timestamp": logged_at,
            "_logged_at": logged_at,
            "_record_origin": "new-pipeline",
        }
        write_audit(new_id, audit_data)

        entry = _row_from_record({
            **audit_data,
            "_source": "shard",
            "_logged_month": audit_store.month_of(logged_at),
            "_written_month": audit_store.month_of(logged_at),
            "_origin_record_no": record_no,
            "_origin_audit_id": new_id,
            "_timezone": audit_store.TZ_LABEL,
            "status": "已记录",
            "abnormal": response_code != "200",
        }, fallback_id=new_id)
        audit_rows.append(entry)
        return entry

    def _now_iso(self) -> str:
        return audit_store.now_iso()

    # ---- 查看 ----

    def list_entries(self, *, filters: dict[str, str] | None = None,
                     keyword: str | None = None, status: str | None = None,
                     page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        rows = self._merged_rows()
        filters = filters or {}
        for field, expected in filters.items():
            if field in self.LIST_FIELDS:
                rows = [
                    row for row in rows
                    if expected.casefold() in str(row.get(field, "")).casefold()
                ]
        if keyword and self.LIST_FIELDS:
            field = self.LIST_FIELDS[0]
            rows = [row for row in rows if keyword.casefold() in str(row.get(field, "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def _merged_rows(self) -> list[dict[str, Any]]:
        """旧目录 ∪ 月分片 ∪ 内存样例，按原始编号去重后统一成列表行。"""
        records, _ = audit_store.query_audit()
        rows = [_row_from_record(record) for record in records]
        # 磁盘记录的编号集合；只有编号相同才认为内存样例已经落盘，避免
        # 仅仅是数字 id 碰撞（样例 id 与新分片 id 都从 1 开始）就误删内存行。
        disk_record_nos = {
            str(value)
            for record in records
            for value in (record.get("_origin_record_no"), audit_store.record_no(record),
                          record.get("记录编号"))
            if value not in (None, "")
        }
        disk_audit_ids = {
            str(record.get("_origin_audit_id"))
            for record in records
            if record.get("_origin_audit_id") is not None
        }
        for memory_row in store.rows(self.MODULE):
            memory_no = str(memory_row.get("记录编号") or "")
            memory_id = str(memory_row.get("id"))
            if memory_no and memory_no in disk_record_nos:
                continue
            if not memory_no and memory_id in disk_audit_ids:
                continue
            row = dict(memory_row)
            row.setdefault("原始编号", memory_row.get("记录编号"))
            row.setdefault("数据来源", "内存样例")
            row.setdefault("归属月份", audit_store.month_of(memory_row.get("时间戳")))
            row.setdefault("写入月份", None)
            row.setdefault("原始时间戳", memory_row.get("时间戳"))
            row.setdefault("时区", audit_store.TZ_LABEL)
            rows.append(row)
        rows.sort(key=lambda r: str(r.get("时间戳") or ""))
        return rows

    def query_range(self, start_time: str | None = None, end_time: str | None = None,
                    api_name: str | None = None, caller: str | None = None,
                    *, end_exclusive: bool = False) -> dict[str, Any]:
        """按区间查看审计明细（跨月），返回记录与对账信息。"""
        records, reconciliation = audit_store.query_audit(
            api_name=api_name,
            caller=caller,
            start_time=start_time,
            end_time=end_time,
            end_exclusive=end_exclusive,
        )
        return {
            "items": [_row_from_record(record) for record in records],
            "records": records,
            "reconciliation": reconciliation,
            "timezone": audit_store.TZ_LABEL,
        }

    def verify_audits(self, caller: str, api_name: str) -> list[dict[str, Any]]:
        """校验调用方对指定API的审计记录。"""
        from app.services.data_manager import list_audit, read_api_snapshot

        registry = read_api_snapshot()
        api_exists = any(
            isinstance(v, dict) and v.get("api_name") == api_name
            for v in (registry.values() if isinstance(registry, dict) else []))

        if not api_exists:
            return []

        audits = list_audit(api_name=api_name, caller=caller)
        from app.services.data_manager import list_subscriptions
        subs = list_subscriptions()
        valid_sub = any(
            s.get("api_name") == api_name and s.get("applicant") == caller
            and s.get("approval_status") == "已通过"
            for s in subs
        )

        for audit in audits:
            audit["subscription_valid"] = valid_sub
            audit["verified"] = valid_sub
        return audits

    # ---- 归档 ----

    def archive_old_records(self, before_date: str) -> dict[str, Any]:
        """归档指定日期（东八区自然日）之前的审计明细。"""
        return audit_store.archive_audit_records(before_date)

    def archive_preview(self, before_date: str) -> dict[str, Any]:
        """归档前预览：按原日期切历史明细，核对跨月归属。"""
        result = self.query_range(end_time=before_date, end_exclusive=True)
        records = result["records"]
        month_counts: dict[str, int] = {}
        for record in records:
            month = record.get("_logged_month") or "unknown"
            month_counts[month] = month_counts.get(month, 0) + 1
        return {
            "before_date": before_date,
            "timezone": audit_store.TZ_LABEL,
            "items": result["items"],
            "month_counts": dict(sorted(month_counts.items())),
            "reconciliation": result["reconciliation"],
        }

    # ---- 迁移 ----

    def migration_status(self) -> dict[str, Any]:
        return audit_store.shard_overview()

    def migrate_legacy(self, months: list[str] | None = None, *,
                       per_month_limit: int | None = None,
                       remove_legacy: bool = False) -> dict[str, Any]:
        return audit_store.migrate_legacy_audit(
            months, per_month_limit=per_month_limit, remove_legacy=remove_legacy)

    # ---- 边界样本核对 ----

    def boundary_samples(self, *, seed: bool = True) -> dict[str, Any]:
        """三个边界样本：迁移前月末、刚跨月、切换过程中新增。

        在“查看”和“归档”两个入口分别核对记录归属。样本幂等写入：
        固定原始编号，重复调用不会产生重复记录。
        """
        if seed:
            audit_store.seed_boundary_samples()
        before = "2026-11-01"
        view = self.query_range(start_time="2026-10-01", end_time="2026-11-30")
        archive = self.query_range(end_time=before, end_exclusive=True)
        archive_months: dict[str, int] = {}
        for record in archive["records"]:
            month = record.get("_logged_month") or "unknown"
            archive_months[month] = archive_months.get(month, 0) + 1
        targets = set(audit_store.SAMPLE_RECORD_NOS)
        return {
            "timezone": audit_store.TZ_LABEL,
            "samples": audit_store.boundary_sample_expectations(),
            "view": {
                "items": [item for item in view["items"] if item.get("原始编号") in targets],
                "reconciliation": view["reconciliation"],
            },
            "archive": {
                "before_date": before,
                "items": [item for item in archive["items"] if item.get("原始编号") in targets],
                "month_counts": dict(sorted(archive_months.items())),
                "reconciliation": archive["reconciliation"],
            },
        }

    # ---- 动作 ----

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "提交归档":
            self.archive_old_records(self._today_str())

        result = super().run_action(entry_id, action)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def _today_str(self) -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        audit_list = []
        for row in rows:
            audit_list.append({
                "id": row.get("id"),
                "record_no": row.get("记录编号"),
                "caller": row.get("调用方"),
                "api": row.get("目标API"),
                "params": row.get("请求参数"),
                "response_code": row.get("响应码"),
                "latency": row.get("响应耗时"),
                "result_rows": row.get("返回行数"),
                "timestamp": row.get("时间戳"),
                "status": row.get("status"),
            })
        overview = audit_store.shard_overview()
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(audit_list),
            "audits": audit_list,
            "archived_count": sum(1 for r in audit_list if r.get("status") == "已归档"),
            "abnormal_count": sum(1 for r in rows if r.get("abnormal")),
            "shards": {
                "timezone": overview["timezone"],
                "months": [
                    {"month": item["month"], "status": item.get("status"),
                     "record_count": item.get("record_count")}
                    for item in overview["months"]
                ],
                "legacy_files": overview["legacy_files"],
            },
        }
