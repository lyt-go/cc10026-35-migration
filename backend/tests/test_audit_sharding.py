"""调用审计按月分片迁移：去重、边界、归档与对账测试。

覆盖：
1. 同一次调用同时出现在旧目录和月分片 → 只算一笔。
2. 迁移未完成的分片（partial）不会让旧目录里的历史行消失。
3. 东八区月份边界：原始时间戳带其他偏移时按 +08:00 归属。
4. 归档按原日期切历史明细，保留原编号、原时间戳和实际写入月份。
5. 三个边界样本在“查看 / 归档”两个入口的归属一致。
"""

import os
import sys
import tempfile
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# data_manager 在导入时读取 DATA_ROOT，必须先设置再导入 app 包。
if "app.services.data_manager" not in sys.modules:
    os.environ.setdefault("DATA_ROOT", tempfile.mkdtemp(prefix="audit_shard_test_"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data_root(tmp_path, monkeypatch):
    """每个用例独立数据目录，避免相互污染。"""
    import app.services.data_manager as dm

    monkeypatch.setattr(dm, "DATA_ROOT", tmp_path / "data")
    yield tmp_path / "data"


def _svc():
    from app.services.call_audit import CallAuditService

    return CallAuditService()


def _legacy(audit_id, timestamp, **extra):
    from app.services import audit_store as store

    record = {
        "audit_id": audit_id,
        "record_no": f"CALL-{audit_id:04d}",
        "caller": extra.pop("caller", "调用方A"),
        "api_name": extra.pop("api_name", "API-A"),
        "timestamp": timestamp,
        "_logged_at": timestamp,
        "status": "已记录",
        **extra,
    }
    store._write_json(store.audit_dir() / f"{audit_id:06d}.json", record)
    return record


# ---------------------------------------------------------------------------
# 1. 双来源去重
# ---------------------------------------------------------------------------


def test_duplicate_across_legacy_and_shard_counts_once():
    from app.services import audit_store as store

    _legacy(1, "2026-10-15T10:00:00+08:00")
    # 迁移流水线把同一笔复制到月分片
    store.migrate_legacy_audit(["2026-10"])

    records, reconciliation = store.query_audit(
        start_time="2026-10-01", end_time="2026-10-31")

    assert len(records) == 1
    assert records[0]["_source"] == "legacy+shard"
    assert records[0]["record_no"] == "CALL-0001"
    assert records[0]["_origin_audit_id"] == 1
    assert reconciliation["legacy_records"] == 1
    assert reconciliation["shard_records"] == 1
    assert reconciliation["unique_records"] == 1
    assert reconciliation["duplicates_removed"] == 1
    assert reconciliation["balanced"] is True


def test_archive_marks_both_copies_but_archives_one_row():
    from app.services import audit_store as store

    _legacy(1, "2026-10-15T10:00:00+08:00")
    store.migrate_legacy_audit(["2026-10"])

    result = store.archive_audit_records("2026-11-01")

    assert result["archived_count"] == 1
    assert result["duplicates_removed"] >= 1
    row = result["records"][0]
    assert row["origin_record_no"] == "CALL-0001"
    assert row["source"] == "legacy+shard"
    assert row["archive_month"] == "2026-10"
    assert row["written_month"] == "2026-10"
    assert row["original_timestamp"] == "2026-10-15T10:00:00+08:00"
    # 归档后两套来源都标记为已归档，再查仍只算一笔
    records, _ = store.query_audit(start_time="2026-10-01", end_time="2026-10-31")
    assert len(records) == 1
    assert records[0]["status"] == "已归档"


# ---------------------------------------------------------------------------
# 2. 未迁完的分片不让历史行消失
# ---------------------------------------------------------------------------


def test_partial_migration_keeps_legacy_rows_visible():
    from app.services import audit_store as store

    for audit_id in range(1, 6):
        _legacy(audit_id, f"2026-10-0{audit_id}T10:00:00+08:00")

    summary = store.migrate_legacy_audit(["2026-10"], per_month_limit=2)
    assert summary["copied"] == 2
    assert summary["months"]["2026-10"]["status"] == "partial"

    records, reconciliation = store.query_audit(
        start_time="2026-10-01", end_time="2026-10-31")
    assert len(records) == 5, "旧目录剩余行必须仍然可见"
    assert reconciliation["legacy_records"] + reconciliation["shard_records"] > 5
    assert reconciliation["unique_records"] == 5
    assert reconciliation["partial_months"] == ["2026-10"]
    assert reconciliation["legacy_remaining"] == 3
    assert reconciliation["balanced"] is True
    assert reconciliation["complete"] is False

    # 补齐迁移后不再有重复，完整性转为 true
    store.migrate_legacy_audit(["2026-10"])
    records, reconciliation = store.query_audit(
        start_time="2026-10-01", end_time="2026-10-31")
    assert len(records) == 5
    assert reconciliation["legacy_remaining"] == 0
    assert reconciliation["complete"] is True


def test_migration_is_idempotent():
    from app.services import audit_store as store

    _legacy(1, "2026-10-15T10:00:00+08:00")
    first = store.migrate_legacy_audit()
    second = store.migrate_legacy_audit()

    assert first["copied"] == 1
    assert second["copied"] == 0
    assert second["skipped"] == 1
    assert store.shard_overview()["months"][0]["record_count"] == 1


# ---------------------------------------------------------------------------
# 3. 时区边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw_ts,expected_month", [
    ("2026-10-31T23:30:00+08:00", "2026-10"),
    ("2026-10-31T16:20:00+00:00", "2026-11"),   # 东八区 11-01 00:20
    ("2026-10-31T20:00:00-05:00", "2026-11"),   # 东八区 11-01 09:00
    ("2026-10-31T15:59:59+00:00", "2026-10"),   # 东八区 10-31 23:59:59
    ("2026-11-01T00:00:00+08:00", "2026-11"),
])
def test_month_boundary_uses_plus_8(raw_ts, expected_month):
    from app.services import audit_store as store

    _legacy(1, raw_ts)
    summary = store.migrate_legacy_audit()
    assert summary["months"].keys() == {expected_month}

    records, _ = store.query_audit()
    assert records[0]["_logged_month"] == expected_month


def test_archive_cutoff_is_east8_midnight():
    from app.services import audit_store as store

    _legacy(1, "2026-10-31T23:30:00+08:00")
    _legacy(2, "2026-10-31T16:20:00+00:00")   # 东八区已进入 11-01

    preview = store.query_audit(end_time="2026-11-01", end_exclusive=True)
    assert [r["record_no"] for r in preview[0]] == ["CALL-0001"]
    assert preview[1]["duplicates_removed"] == 0

    result = store.archive_audit_records("2026-11-01")
    assert result["archived_count"] == 1
    assert result["month_counts"] == {"2026-10": 1}
    # 11 月的记录不能被十月归档带走
    remaining, _ = store.query_audit(start_time="2026-11-01", end_time="2026-11-30")
    assert [r["record_no"] for r in remaining] == ["CALL-0002"]


# ---------------------------------------------------------------------------
# 4. 跨月查询与归档对账
# ---------------------------------------------------------------------------


def test_cross_month_query_and_archive_reconcile():
    from app.services import audit_store as store

    _legacy(1, "2026-10-05T09:00:00+08:00")
    _legacy(2, "2026-10-25T09:00:00+08:00")
    _legacy(3, "2026-11-02T09:00:00+08:00")
    store.migrate_legacy_audit(["2026-10"])
    # 切换后新调用只进分片
    store.write_audit_record(4, {
        "audit_id": 4, "record_no": "CALL-0004", "caller": "调用方A",
        "api_name": "API-A", "timestamp": "2026-11-06T09:00:00+08:00",
    })

    records, reconciliation = store.query_audit(
        start_time="2026-10-01", end_time="2026-11-30")
    assert len(records) == 4
    assert reconciliation["month_counts"] == {"2026-10": 2, "2026-11": 2}
    assert reconciliation["duplicates_removed"] == 2
    assert reconciliation["expected_months"] == ["2026-10", "2026-11"]

    archive = store.archive_audit_records("2026-11-01")
    assert archive["archived_count"] == 2
    assert archive["month_counts"] == {"2026-10": 2}
    assert archive["source_counts"]["both"] == 2
    assert archive["reconciliation"]["unique_records"] == 2

    # 归档文件按月份切开，且保留原编号
    month_file = store.audit_dir() / "archive" / "2026-11-01" / "2026-10.json"
    import json
    payload = json.loads(month_file.read_text(encoding="utf-8"))
    assert {row["origin_record_no"] for row in payload["records"]} == {"CALL-0001", "CALL-0002"}
    assert payload["timezone"] == "+08:00"

    # 预览改动后不产生重复
    preview = _svc().archive_preview("2026-11-01")
    assert preview["month_counts"] == {"2026-10": 2}


def test_archive_keeps_records_when_shards_incomplete():
    from app.services import audit_store as store

    for audit_id in range(1, 5):
        _legacy(audit_id, f"2026-10-0{audit_id}T10:00:00+08:00")
    store.migrate_legacy_audit(["2026-10"], per_month_limit=1)

    result = store.archive_audit_records("2026-11-01")
    assert result["archived_count"] == 4, "分片只转了一部分时，旧目录里的其余行仍要归档"
    assert result["source_counts"]["legacy"] == 3
    assert result["source_counts"]["both"] == 1


# ---------------------------------------------------------------------------
# 5. 三个边界样本在查看 / 归档入口一致
# ---------------------------------------------------------------------------


def test_boundary_samples_view_and_archive():
    svc = _svc()
    result = svc.boundary_samples(seed=True)

    view = {item["原始编号"]: item for item in result["view"]["items"]}
    archived = {item["原始编号"]: item for item in result["archive"]["items"]}

    # 1. 迁移前月末：只在旧目录，归属 10 月，会被 11-01 归档收走
    assert view["CALL-SAMPLE-0001"]["归属月份"] == "2026-10"
    assert view["CALL-SAMPLE-0001"]["数据来源"] == "legacy"
    assert view["CALL-SAMPLE-0001"]["写入月份"] is None
    assert "CALL-SAMPLE-0001" in archived

    # 2. 刚跨月：两套来源各一份，只算一笔，按东八区归属 11 月
    assert view["CALL-SAMPLE-0002"]["归属月份"] == "2026-11"
    assert view["CALL-SAMPLE-0002"]["数据来源"] == "legacy+shard"
    assert view["CALL-SAMPLE-0002"]["写入月份"] == "2026-11"
    assert view["CALL-SAMPLE-0002"]["时间戳"] == "2026-10-31T16:20:00+00:00"
    assert "CALL-SAMPLE-0002" not in archived

    # 3. 切换过程中新增：只在新分片，归属/写入均为 11 月
    assert view["CALL-SAMPLE-0003"]["归属月份"] == "2026-11"
    assert view["CALL-SAMPLE-0003"]["数据来源"] == "shard"
    assert view["CALL-SAMPLE-0003"]["写入月份"] == "2026-11"
    assert "CALL-SAMPLE-0003" not in archived

    assert set(view) == {"CALL-SAMPLE-0001", "CALL-SAMPLE-0002", "CALL-SAMPLE-0003"}
    assert result["view"]["reconciliation"]["duplicates_removed"] == 1
    assert result["archive"]["month_counts"] == {"2026-10": 1}

    # 幂等：重复核对不会产生新记录
    again = svc.boundary_samples(seed=True)
    assert again["view"]["reconciliation"]["unique_records"] == 3
    assert again["view"]["reconciliation"]["duplicates_removed"] == 1


def test_service_list_and_new_call_go_to_shard():
    from app.services import audit_store as store

    svc = _svc()
    entry = svc.record_api_call("调用方B", "API-B", timestamp="2026-11-06T10:00:00+08:00")

    shard = store.shard_dir() / "2026-11"
    assert (shard / f"{entry['id']:06d}.json").exists()
    assert entry["归属月份"] == "2026-11"
    assert entry["写入月份"] == "2026-11"
    assert entry["数据来源"] == "shard"

    items, total = svc.list_entries()
    # 3 条内存样例 + 1 条新分片记录
    assert total == 4
    target = [item for item in items if item["原始编号"] == entry["记录编号"]]
    assert len(target) == 1
    assert target[0]["数据来源"] == "shard"

    # 下一笔编号跨旧目录和分片连续
    assert store.next_audit_id() == entry["id"] + 1


def test_archive_status_visible_from_both_sources():
    """归档写回两套来源：分片优先也不会读到旧状态。"""
    from app.services import audit_store as store

    _legacy(1, "2026-10-15T10:00:00+08:00")
    store.migrate_legacy_audit(["2026-10"])
    store.archive_audit_records("2026-11-01")

    records, _ = store.query_audit(start_time="2026-10-01", end_time="2026-10-31")
    assert records[0]["status"] == "已归档"
    shard = json.loads((store.shard_dir() / "2026-10" / "000001.json").read_text(encoding="utf-8"))
    legacy = json.loads((store.audit_dir() / "000001.json").read_text(encoding="utf-8"))
    assert shard["status"] == "已归档"
    assert legacy["status"] == "已归档"
    assert shard["record_no"] == legacy["record_no"] == "CALL-0001"


def test_unknown_record_without_id_merges_only_across_sources():
    """无编号记录只在跨来源时按指纹合并，同源重复调用仍计两笔。"""
    from app.services import audit_store as store

    base = {"caller": "调用方C", "api_name": "API-C", "timestamp": "2026-10-10T10:00:00+08:00",
            "_logged_at": "2026-10-10T10:00:00+08:00", "event": "export_completed"}
    store._write_json(store.audit_dir() / "000101.json", dict(base))
    store._write_json(store.audit_dir() / "000102.json", dict(base))
    store.write_shard_record(dict(base))

    records, reconciliation = store.query_audit(
        start_time="2026-10-01", end_time="2026-10-31")
    # 两次旧目录调用 + 一次分片副本 → 分片副本与第一份旧目录合并，另一份仍是独立一笔
    assert len(records) == 2
    assert reconciliation["unique_records"] == 2
    assert reconciliation["legacy_records"] == 2
    assert reconciliation["shard_records"] == 1
