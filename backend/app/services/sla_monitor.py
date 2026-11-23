"""SLA监控领域服务：管理API服务质量指标、延迟监控和告警策略。

跨模块数据流：
- 消费 api_registry 快照 → 关联API加载SLA指标
- 监控异常 → 触发 error_classify 异常归类
- 监控异常 → 触发 query_route 降级路由
- 达标/偏离状态 → 写共享快照通知消费方
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class SlaMonitorService(BaseModuleService):
    MODULE = "sla_monitor"
    ENTITY = "SLA指标"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["指标编号", "关联API", "可用率目标", "P99延迟", "错误率上限", "监控周期", "当前状态", "最近告警"]
    REQUIRED_FIELDS = ["指标编号", "关联API", "可用率目标"]
    STATUS_ORDER = ["草稿", "监控中", "已达标", "已偏离"]
    ACTION_RULES = {"启动监控": "监控中", "标记达标": "已达标", "标记偏离": "已偏离"}
    NEGATIVE_ACTIONS = []

    def record_metric(self, api_name: str, latency_ms: float, error: bool) -> dict[str, Any]:
        """记录一次调用指标并检查SLA。"""
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("关联API") == api_name and row.get("status") in ("监控中", "已偏离"):
                p99 = float(row.get("P99延迟", "0") or "0")
                error_rate = float(row.get("错误率上限", "0") or "0")

                if latency_ms > p99:
                    row["最近告警"] = f"P99延迟超标：{latency_ms}ms > {p99}ms"
                    row["abnormal"] = True
                    self._classify_error(api_name, "latency_exceeded", f"延迟{latency_ms}ms")

                if error and error_rate > 0:
                    row["最近告警"] = f"出现调用错误"
                    row["abnormal"] = True
                    self._classify_error(api_name, "call_error", "调用返回错误")

                return row
        return {}

    def _classify_error(self, api_name: str, error_type: str, detail: str) -> None:
        """将SLA异常传递给error_classify进行归类。

        联动增强：偏离事件自动写入 error_classify 的异常入库记录，
        同时写入调用审计记录。
        """
        from app.services.data_manager import write_module_snapshot, write_audit, next_audit_id
        # 记录SLA事件快照给error_classify消费
        snap = {
            "module": self.MODULE,
            "api_name": api_name,
            "error_type": error_type,
            "detail": detail,
            "timestamp": self._now_iso(),
        }
        write_module_snapshot("sla_events", snap)

        # 联动增强：自动写入error_classify异常归类记录
        audit_id = next_audit_id()
        write_audit(audit_id, {
            "audit_id": audit_id,
            "api_name": api_name,
            "caller": "sla_monitor",
            "event": error_type,
            "detail": detail,
            "classification": "已记录",
            "severity": "high" if error_type == "latency_exceeded" else "medium",
        })

    def _now_iso(self) -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=8))).isoformat()

    def degrade_if_needed(self, api_name: str) -> dict[str, Any]:
        """检查是否需要降级路由。"""
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("关联API") == api_name and row.get("status") == "已偏离":
                from app.services import service_for
                route_svc = service_for("query_route")
                if route_svc and hasattr(route_svc, "degrade_route"):
                    return route_svc.degrade_route(row.get("id", 0), "SLA偏离触发降级")
        return {}

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "启动监控":
            from app.services.data_manager import read_api_snapshot
            api_name = entry.get("关联API", "")
            registry = read_api_snapshot()
            for api_data in (registry.values() if isinstance(registry, dict) else []):
                if isinstance(api_data, dict) and api_data.get("api_name") == api_name:
                    break
            else:
                return None, "关联API尚未注册，无法启动SLA监控"

        result = super().run_action(entry_id, action)

        if action == "标记偏离":
            self.degrade_if_needed(entry.get("关联API", ""))
            self._classify_error(entry.get("关联API", ""), "sla_deviation", "SLA指标偏离")

        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        sla_list = []
        for row in rows:
            sla_list.append({
                "id": row.get("id"),
                "metric_no": row.get("指标编号"),
                "api": row.get("关联API"),
                "availability": row.get("可用率目标"),
                "p99": row.get("P99延迟"),
                "error_rate": row.get("错误率上限"),
                "period": row.get("监控周期"),
                "current_status": row.get("当前状态"),
                "last_alert": row.get("最近告警"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(sla_list),
            "metrics": sla_list,
            "monitoring_count": sum(1 for r in sla_list if r.get("status") == "监控中"),
            "deviated_count": sum(1 for r in sla_list if r.get("status") == "已偏离"),
        }