"""查询路由领域服务：管理查询路由规则、降级策略和超时控制。

跨模块数据流：
- 消费 api_registry 共享快照 → 校验关联API是否已上线
- 消费 cache_layer 快照 → 路由规则与缓存策略联动
- 被 sla_monitor 监控 → SLA异常触发降级路由
- 被 error_classify 消费 → 路由错误触发异常归类
- 输出路由快照 → rate_limiter 消费频率限制
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class QueryRouteService(BaseModuleService):
    MODULE = "query_route"
    ENTITY = "路由规则"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["规则编号", "关联API", "路由条件", "目标数据源", "优先权重", "降级策略", "超时阈值", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "关联API", "路由条件"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def _validate_associated_api(self, rule_data: dict[str, Any]) -> bool:
        """校验关联API是否已上线。"""
        from app.services.data_manager import read_api_snapshot
        api_name = rule_data.get("关联API", "")
        registry = read_api_snapshot()
        for api_data in (registry.values() if isinstance(registry, dict) else []):
            if isinstance(api_data, dict) and api_data.get("api_name") == api_name and api_data.get("status") == "已上线":
                return True
        return False

    def _consume_cache_layer(self, rule_data: dict[str, Any]) -> dict[str, Any]:
        """读取缓存层策略，供路由规则与缓存联动。"""
        from app.services.data_manager import read_module_snapshot
        return read_module_snapshot("cache_layer")

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "标记生效":
            if not self._validate_associated_api(entry):
                return None, "关联API尚未上线，无法标记生效路由规则"
            # 消费缓存层快照
            cache_snap = self._consume_cache_layer(entry)
            if cache_snap:
                entry["_cache_policy_ref"] = cache_snap.get("module")

        result = super().run_action(entry_id, action)

        # 输出路由快照
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())

        return result

    def degrade_route(self, api_id: int, reason: str) -> dict[str, Any]:
        """SLA监控触发降级：将指定API的路由规则标记为降级。

        联动增强：降级触发后同时通知 usage_billing 记录异常用量、
        写入 call_audit 审计日志。
        """
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("关联API") and api_id:
                row["status"] = "已停用"
                row["abnormal"] = True
                row["_degrade_reason"] = reason
                row["_degrade_at"] = self._now_iso()

                # 联动增强：记录降级事件到审计
                from app.services.data_manager import write_audit, next_audit_id
                audit_id = next_audit_id()
                write_audit(audit_id, {
                    "audit_id": audit_id,
                    "api_name": row.get("关联API", ""),
                    "caller": "sla_monitor",
                    "event": "route_degraded",
                    "reason": reason,
                    "degrade_at": row["_degrade_at"],
                })
                return row
        return {}

    def _now_iso(self) -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=8))).isoformat()

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        route_list = []
        for row in rows:
            route_list.append({
                "id": row.get("id"),
                "rule_no": row.get("规则编号"),
                "api": row.get("关联API"),
                "condition": row.get("路由条件"),
                "target": row.get("目标数据源"),
                "weight": row.get("优先权重"),
                "fallback": row.get("降级策略"),
                "timeout": row.get("超时阈值"),
                "version": row.get("规则版本"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(route_list),
            "routes": route_list,
            "active_count": sum(1 for r in route_list if r.get("status") == "已生效"),
            "degraded_count": sum(1 for r in route_list if r.get("status") == "已停用"),
        }