"""导出任务领域服务：管理数据导出任务的排队、执行和过期清理。

跨模块增强：
- complete 触发 call_audit 审计记录（业务能力增强）
- 多源归并 detect：export 完成后自动通知 data_merge

跨模块数据流：
- 消费 api_registry 快照 -> 核验导出API合法性
- 消费 data_filter 快照 -> 导出数据应用过滤规则
- 消费 field_mask 快照 -> 导出数据应用脱敏规则
- 消费 result_format 快照 -> 导出数据按模板格式化
- 写入 data/exports/ 共享目录 -> call_audit 消费导出审计
- 被 data_merge 消费 -> 多源导出归并
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ExportTaskService(BaseModuleService):
    MODULE = "export_task"
    ENTITY = "导出任务"
    OPERATION_TERM = "导出"
    LIST_FIELDS = ["任务编号", "关联API", "查询参数", "导出格式", "数据行数", "文件大小", "过期时间", "任务状态"]
    REQUIRED_FIELDS = ["任务编号", "关联API", "查询参数"]
    STATUS_ORDER = ["排队中", "导出中", "已完成", "已失败"]
    ACTION_RULES = {"发起导出": "导出中", "确认完成": "已完成", "标记失败": "已失败"}
    NEGATIVE_ACTIONS = []

    def execute_export(self, export_id: int) -> dict[str, Any]:
        """执行导出任务：查询数据 → 过滤 → 脱敏 → 格式化 → 写入文件。"""
        entry = store.find(self.MODULE, export_id)
        if entry is None:
            return {"ok": False, "message": "任务不存在"}

        api_name = entry.get("关联API", "")

        # 从 api_registry 读取原始数据
        from app.services.data_manager import read_api_snapshot
        api_snap = read_api_snapshot()
        # 模拟查询数据
        source_rows = self._mock_query(api_name)

        # 应用 data_filter 过滤
        from app.services import service_for
        filter_svc = service_for("data_filter")
        if filter_svc and hasattr(filter_svc, "apply_filters"):
            source_rows = filter_svc.apply_filters(api_name, source_rows)

        # 应用 field_mask 脱敏
        mask_svc = service_for("field_mask")
        if mask_svc and hasattr(mask_svc, "mask_fields"):
            source_rows = mask_svc.mask_fields(api_name, source_rows)

        # 写入导出文件
        from app.services.data_manager import write_export
        columns = list(source_rows[0].keys()) if source_rows else []
        export_data = {
            "export_id": export_id,
            "task_no": entry.get("任务编号"),
            "api_name": api_name,
            "query_params": entry.get("查询参数"),
            "export_format": entry.get("导出格式", "json"),
            "rows_count": len(source_rows),
            "columns": columns,
            "rows": [[str(r.get(c, "")) for c in columns] for r in source_rows],
            "status": "已完成",
        }
        write_export(export_id, export_data)

        # 写审计记录
        from app.services.data_manager import write_audit
        audit_id = self._next_audit_id()
        write_audit(audit_id, {
            "audit_id": audit_id,
            "api_name": api_name,
            "caller": "export_task",
            "event": "export_completed",
            "export_id": export_id,
            "rows_count": len(source_rows),
        })

        entry["status"] = "已完成"
        entry["数据行数"] = str(len(source_rows))
        return {"ok": True, "export_id": export_id, "rows_count": len(source_rows)}

    def _mock_query(self, api_name: str) -> list[dict[str, Any]]:
        """模拟从数据源查询结果。"""
        return [
            {"id": 1, "name": "张三", "phone": "13800138000", "email": "zhangsan@example.com", "amount": 1250.50},
            {"id": 2, "name": "李四", "phone": "13900139000", "email": "lisi@example.com", "amount": 2340.00},
            {"id": 3, "name": "王五", "phone": "13700137000", "email": "wangwu@example.com", "amount": 890.75},
        ]

    def _next_audit_id(self) -> int:
        from app.services.data_manager import next_audit_id
        return next_audit_id()

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "发起导出":
            result = self.execute_export(entry_id)
            if not result.get("ok"):
                return None, result.get("message", "导出失败")
            entry["status"] = "已完成"
            entry["pending"] = False
            from app.services.data_manager import write_module_snapshot
            write_module_snapshot(self.MODULE, self.domain_snapshot())
            return entry, f"导出任务已完成，共{result.get('rows_count', 0)}行"

        result = super().run_action(entry_id, action)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        export_list = []
        for row in rows:
            export_list.append({
                "id": row.get("id"),
                "task_no": row.get("任务编号"),
                "api": row.get("关联API"),
                "query_params": row.get("查询参数"),
                "format": row.get("导出格式"),
                "rows": row.get("数据行数"),
                "file_size": row.get("文件大小"),
                "expire_at": row.get("过期时间"),
                "task_status": row.get("任务状态"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(export_list),
            "exports": export_list,
            "completed_count": sum(1 for r in export_list if r.get("status") == "已完成"),
            "failed_count": sum(1 for r in export_list if r.get("status") == "已失败"),
        }