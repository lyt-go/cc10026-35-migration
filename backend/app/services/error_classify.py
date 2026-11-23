"""异常归类领域服务：管理API调用异常的分类和分级策略。

跨模块数据流：
- 消费 sla_monitor 快照 → SLA偏离事件触发异常归类
- 消费 call_audit 快照 → 审计异常调用触发归类
- 归类结果供 query_route 降级路由决策
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ErrorClassifyService(BaseModuleService):
    MODULE = "error_classify"
    ENTITY = "异常类型"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["类型编号", "类型名称", "错误码段", "分级策略", "重试建议", "熔断阈值", "通知方式", "分类版本"]
    REQUIRED_FIELDS = ["类型编号", "类型名称", "错误码段"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用分类": "已停用"}
    NEGATIVE_ACTIONS = ["停用分类"]

    def classify(self, error_code: str, error_message: str = "") -> dict[str, Any]:
        """根据错误码进行异常归类。"""
        rows = store.rows(self.MODULE)
        active_classifications = [r for r in rows if r.get("status") == "已生效"]

        for row in active_classifications:
            code_range = row.get("错误码段", "")
            if not code_range:
                continue
            # 支持通配符模式 5XX 匹配 500/501 等
            matched = False
            if code_range.endswith("XX"):
                prefix = code_range[:-2]
                if error_code.startswith(prefix):
                    matched = True
            elif error_code.startswith(code_range):
                matched = True

            if matched:
                return {
                    "matched": True,
                    "type_name": row.get("类型名称"),
                    "severity": row.get("分级策略"),
                    "retry_advice": row.get("重试建议"),
                    "circuit_breaker": row.get("熔断阈值"),
                    "notify": row.get("通知方式"),
                    "error_code": error_code,
                    "error_message": error_message,
                }
        return {"matched": False, "error_code": error_code, "error_message": error_message}

    def consume_sla_events(self) -> list[dict[str, Any]]:
        """消费SLA监控事件快照进行归类。

        联动增强：归类后自动写入审计记录，供下游查询追溯。
        返回归类结果列表。
        """
        from app.services.data_manager import read_module_snapshot, write_audit, next_audit_id
        results = []
        snap = read_module_snapshot("sla_events")
        if snap and snap.get("error_type"):
            classification = self.classify(snap.get("error_type", ""), snap.get("detail", ""))
            # 联动增强：写入审计记录
            if classification.get("matched"):
                audit_id = next_audit_id()
                write_audit(audit_id, {
                    "audit_id": audit_id,
                    "api_name": snap.get("api_name", ""),
                    "caller": "sla_monitor",
                    "event": "error_classified",
                    "type_name": classification.get("type_name"),
                    "severity": classification.get("severity"),
                    "retry_advice": classification.get("retry_advice"),
                })
            results.append(classification)
        return results

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        result = super().run_action(entry_id, action)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        classify_list = []
        for row in rows:
            classify_list.append({
                "id": row.get("id"),
                "type_no": row.get("类型编号"),
                "type_name": row.get("类型名称"),
                "code_range": row.get("错误码段"),
                "severity": row.get("分级策略"),
                "retry": row.get("重试建议"),
                "breaker": row.get("熔断阈值"),
                "notify": row.get("通知方式"),
                "version": row.get("分类版本"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(classify_list),
            "classifications": classify_list,
            "active_count": sum(1 for r in classify_list if r.get("status") == "已生效"),
        }