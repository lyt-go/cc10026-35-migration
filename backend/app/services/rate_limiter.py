"""频率限制领域服务：管理API调用频率限制策略。

跨模块数据流：
- 消费 api_registry 快照 → 校验目标API存在
- 消费 query_route 快照 → 路由权重影响限流策略
- 被 usage_billing 消费 → 超限计入用量异常
- 被 call_audit 消费 → 超限事件触发审计
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class RateLimiterService(BaseModuleService):
    MODULE = "rate_limiter"
    ENTITY = "限流策略"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["策略编号", "目标API", "调用方", "每秒上限", "每分钟上限", "每日上限", "超限动作", "策略版本"]
    REQUIRED_FIELDS = ["策略编号", "目标API", "调用方"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    def _validate_target_api(self, rule_data: dict[str, Any]) -> bool:
        """校验目标API是否已注册。"""
        from app.services.data_manager import read_api_snapshot
        api_name = rule_data.get("目标API", "")
        registry = read_api_snapshot()
        for api_data in (registry.values() if isinstance(registry, dict) else []):
            if isinstance(api_data, dict) and api_data.get("api_name") == api_name:
                return True
        return False

    def check_rate_limit(self, api_name: str, caller: str, rate_type: str = "daily") -> dict[str, Any]:
        """检查调用方是否超过频率限制。返回限流检查结果。"""
        from app.services.data_manager import write_audit
        from app.store import store as s

        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("目标API") == api_name and row.get("调用方") == caller and row.get("status") == "已生效":
                limit_key = {"daily": "每日上限", "minute": "每分钟上限", "second": "每秒上限"}.get(rate_type, "每日上限")
                limit = int(row.get(limit_key, "0") or 0)
                if limit <= 0:
                    return {"allowed": True, "limit": limit, "reason": "无限制"}

                # 检查审计记录中的调用计数
                from app.services.data_manager import list_audit
                today = self._today_str()
                audits = list_audit(api_name=api_name, caller=caller, start_time=today)
                if len(audits) >= limit:
                    # 超限 → 写审计记录
                    audit_id = self._next_audit_id()
                    write_audit(audit_id, {
                        "audit_id": audit_id,
                        "api_name": api_name,
                        "caller": caller,
                        "event": "rate_limit_exceeded",
                        "limit": limit,
                        "current_count": len(audits),
                        "action": row.get("超限动作", "拒绝"),
                    })
                    return {"allowed": False, "limit": limit, "current_count": len(audits), "reason": "超过频率限制"}
                return {"allowed": True, "limit": limit, "current_count": len(audits), "reason": "正常"}
        return {"allowed": True, "limit": 0, "reason": "未配置限流策略"}

    def _next_audit_id(self) -> int:
        from app.services.data_manager import next_audit_id
        return next_audit_id()

    def _today_str(self) -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "标记生效":
            if not self._validate_target_api(entry):
                return None, "目标API尚未注册，无法标记生效限流策略"

        result = super().run_action(entry_id, action)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        rate_list = []
        for row in rows:
            rate_list.append({
                "id": row.get("id"),
                "policy_no": row.get("策略编号"),
                "api": row.get("目标API"),
                "caller": row.get("调用方"),
                "per_second": row.get("每秒上限"),
                "per_minute": row.get("每分钟上限"),
                "per_day": row.get("每日上限"),
                "action": row.get("超限动作"),
                "version": row.get("策略版本"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(rate_list),
            "policies": rate_list,
            "active_count": sum(1 for r in rate_list if r.get("status") == "已生效"),
        }