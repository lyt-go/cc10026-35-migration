"""调用审计分片存储：按月归档审计明细，并兼容历史逐条 JSON。

存储布局（DATA_ROOT 默认仓库根下 data/）：:

    data/audit/
      000123.json                 # 迁移前的存量逐条明细（只读兼容）
      _archive_2026-10-01.json    # 旧目录归档产物（保持路径不变）
      monthly/
        manifest.json             # 迁移状态：每个月的分片是否完整
        2026-09/000900.json       # 按月分片后的逐条明细
        2026-10/001000.json
      archive/
        2026-11-01/2026-09.json   # 按原日期切出的历史明细

关键约束：

* 月份一律按东八区（UTC+08:00）切分。记录里的原始时间戳（含原始时区偏移）
  原样保留，只在归属月份时换算到 +08:00。
* 同一次调用可能同时存在于旧目录和月分片。查询时按“原始编号 / 原始
  audit_id”归并，只计一笔，并标注来源（legacy / shard / both）。
* 未迁移完的分片（manifest 标记 partial）不会让旧目录里的历史行消失：
  查询始终取“旧目录 ∪ 月分片”，分片只负责去重和补全字段。
* 归档按原始日期切历史明细，结果保留原编号、原时间戳和实际写入月份。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# 东八区：月分片的唯一边界时区
TZ = timezone(timedelta(hours=8))
TZ_LABEL = "+08:00"

AUDIT_DIRNAME = "audit"
SHARD_DIRNAME = "monthly"
ARCHIVE_DIRNAME = "archive"
MANIFEST_NAME = "manifest.json"

_IDENTITY_FIELDS = ("record_no", "记录编号", "record_id")


# ---------------------------------------------------------------------------
# 基础读写
# ---------------------------------------------------------------------------


def _data_root() -> Path:
    # 延迟导入，避免 data_manager <-> audit_store 的循环依赖。
    from app.services.data_manager import DATA_ROOT

    return DATA_ROOT


def audit_dir() -> Path:
    return _data_root() / AUDIT_DIRNAME


def shard_dir() -> Path:
    return audit_dir() / SHARD_DIRNAME


def now_iso() -> str:
    return datetime.now(TZ).isoformat()


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, data: Any) -> None:
    _ensure_dir(path.parent)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 时间与时区边界
# ---------------------------------------------------------------------------


def parse_ts(value: Any) -> datetime | None:
    """解析时间戳并换算到东八区；无法解析时返回 None。"""
    if value is None or value == "":
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y%m%d%H%M%S"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def record_ts(record: dict[str, Any]) -> datetime | None:
    for field in ("_logged_at", "timestamp", "_updated_at", "_created_at"):
        parsed = parse_ts(record.get(field))
        if parsed is not None:
            return parsed
    return None


def month_of(value: Any) -> str | None:
    parsed = parse_ts(value)
    return parsed.strftime("%Y-%m") if parsed else None


def _start_bound(value: str | None) -> datetime | None:
    parsed = parse_ts(value)
    return parsed


def _end_bound(value: str | None, *, exclusive: bool) -> tuple[datetime | None, bool]:
    """返回 (边界, 是否闭区间)。

    - 纯日期：自然日语义；非 exclusive 时包含当天整天，exclusive 时从当天零点排除。
    - 完整时间戳：默认闭区间；exclusive 时严格小于。
    """
    parsed = parse_ts(value)
    if parsed is None:
        return None, True
    is_date_only = len(str(value).strip()) <= 10
    if is_date_only:
        if exclusive:
            return parsed, False
        return parsed + timedelta(days=1), False
    return parsed, not exclusive


def _in_range(ts: datetime | None, start_dt: datetime | None,
              end_dt: datetime | None, end_inclusive: bool) -> bool:
    if start_dt is None and end_dt is None:
        return True
    if ts is None:
        return False
    if start_dt is not None and ts < start_dt:
        return False
    if end_dt is not None:
        if end_inclusive and ts > end_dt:
            return False
        if not end_inclusive and ts >= end_dt:
            return False
    return True


def _months_between(start_dt: datetime | None, end_dt: datetime | None) -> list[str]:
    if start_dt is None or end_dt is None:
        return []
    cursor = start_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # end_dt 可能是下月零点（闭区间换算后的排他边界），回退 1 微秒避免多算一个月。
    last = (end_dt - timedelta(microseconds=1))
    if last < cursor:
        return []
    months: list[str] = []
    while cursor <= last:
        months.append(cursor.strftime("%Y-%m"))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


# ---------------------------------------------------------------------------
# 身份：原始编号 / 原始 audit_id
# ---------------------------------------------------------------------------


def record_no(record: dict[str, Any]) -> str | None:
    for field in _IDENTITY_FIELDS:
        value = record.get(field)
        if value not in (None, ""):
            return str(value)
    return None


def origin_audit_id(record: dict[str, Any]) -> Any:
    value = record.get("audit_id")
    if value is not None:
        return value
    raw = record.get("id")
    return raw


def _content_fingerprint(record: dict[str, Any]) -> str | None:
    """没有原始编号时的兜底指纹：仅用于跨来源识别同一笔，不做同源合并。"""
    parts = [
        str(record.get("caller") or record.get("调用方") or ""),
        str(record.get("api_name") or record.get("目标API") or ""),
        str(record.get("request_params") or record.get("请求参数") or ""),
        str(record.get("timestamp") or record.get("时间戳") or record.get("_logged_at") or ""),
    ]
    if not any(parts):
        return None
    return "fp:" + "|".join(parts)


def identity_keys(record: dict[str, Any]) -> list[str]:
    """同一笔调用的所有可用身份键，用于跨来源合并。"""
    keys: list[str] = []
    rec_no = record_no(record)
    if rec_no:
        keys.append(f"no:{rec_no}")
    audit_id = origin_audit_id(record)
    if audit_id is not None and audit_id != "":
        keys.append(f"id:{audit_id}")
    return keys


class _DisjointSet:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def add(self, item: int) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: int) -> int:
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            parent = self.parent[item]
            self.parent[item] = root
            item = parent
        return root

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


# ---------------------------------------------------------------------------
# 分片清单
# ---------------------------------------------------------------------------


def read_manifest() -> dict[str, Any]:
    manifest = _read_json(shard_dir() / MANIFEST_NAME, None)
    if not isinstance(manifest, dict):
        manifest = {}
    manifest.setdefault("version", 1)
    manifest.setdefault("timezone", TZ_LABEL)
    manifest.setdefault("months", {})
    if not isinstance(manifest.get("months"), dict):
        manifest["months"] = {}
    return manifest


def write_manifest(manifest: dict[str, Any]) -> None:
    manifest["timezone"] = TZ_LABEL
    manifest["updated_at"] = now_iso()
    _write_json(shard_dir() / MANIFEST_NAME, manifest)


def update_month_manifest(month: str, **values: Any) -> dict[str, Any]:
    manifest = read_manifest()
    entry = manifest["months"].get(month) or {}
    entry.update(values)
    entry["timezone"] = TZ_LABEL
    manifest["months"][month] = entry
    write_manifest(manifest)
    return manifest


def month_status(month: str) -> str:
    entry = read_manifest()["months"].get(month) or {}
    return str(entry.get("status") or "unregistered")


# ---------------------------------------------------------------------------
# 分片写入
# ---------------------------------------------------------------------------


def _safe_slug(value: Any) -> str:
    text = str(value)
    if re.fullmatch(r"\d+", text):
        return f"{int(text):06d}"
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", text) or "record"


def _month_ts(record: dict[str, Any]) -> datetime:
    ts = record_ts(record)
    if ts is None:
        ts = datetime.now(TZ)
    return ts


def shard_path_for(record: dict[str, Any], month: str | None = None) -> Path:
    target_month = month or _month_ts(record).strftime("%Y-%m")
    slug = _safe_slug(origin_audit_id(record) or record_no(record) or "record")
    return shard_dir() / target_month / f"{slug}.json"


def write_shard_record(record: dict[str, Any], month: str | None = None) -> Path:
    """把一条审计明细写入对应月份的分片，保留原始编号和原始时间戳。"""
    payload = dict(record)
    ts = _month_ts(payload)
    target_month = month or ts.strftime("%Y-%m")
    payload.setdefault("_logged_at", ts.isoformat())
    if payload.get("record_no") in (None, ""):
        resolved_no = record_no(payload)
        if resolved_no:
            payload["record_no"] = resolved_no
    payload["_shard_month"] = target_month
    # 实际写入月份只在第一次落分片时记录，后续更新不抹掉原始写入月份。
    payload.setdefault("_written_month", target_month)
    payload.setdefault("_written_at", now_iso())
    payload["_storage"] = "monthly-shard"
    path = shard_path_for(payload, target_month)
    _write_json(path, payload)
    update_month_manifest(
        target_month,
        record_count=_count_shard_month(target_month),
        status=month_status(target_month) if month_status(target_month) != "unregistered" else "active",
        last_written_at=payload["_written_at"],
    )
    return path


def _count_shard_month(month: str) -> int:
    directory = shard_dir() / month
    if directory.is_dir():
        return len(list(directory.glob("*.json")))
    file_path = shard_dir() / f"{month}.json"
    data = _read_json(file_path, None)
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        return len(data["records"])
    return 0


def shard_months() -> list[str]:
    months: set[str] = set(read_manifest()["months"].keys())
    directory = shard_dir()
    if directory.is_dir():
        for child in directory.iterdir():
            if child.is_dir() and re.fullmatch(r"\d{4}-\d{2}", child.name):
                months.add(child.name)
            elif child.suffix == ".json" and re.fullmatch(r"\d{4}-\d{2}", child.stem):
                months.add(child.stem)
    return sorted(months)


def _read_shard_entries(months: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """读取月分片，返回 [{record, month, path}]。"""
    wanted = sorted(set(months)) if months is not None else shard_months()
    entries: list[dict[str, Any]] = []
    for month in wanted:
        directory = shard_dir() / month
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                record = _read_json(path, None)
                if isinstance(record, dict) and record:
                    entries.append({"record": record, "month": month, "path": path})
            continue
        file_path = shard_dir() / f"{month}.json"
        data = _read_json(file_path, None)
        if isinstance(data, list):
            for index, record in enumerate(data):
                if isinstance(record, dict) and record:
                    entries.append({"record": record, "month": month,
                                    "path": file_path, "index": index})
        elif isinstance(data, dict) and isinstance(data.get("records"), list):
            for index, record in enumerate(data["records"]):
                if isinstance(record, dict) and record:
                    entries.append({"record": record, "month": month,
                                    "path": file_path, "index": index})
    return entries


# ---------------------------------------------------------------------------
# 旧逐条 JSON
# ---------------------------------------------------------------------------


def _legacy_paths() -> list[Path]:
    directory = audit_dir()
    if not directory.is_dir():
        return []
    paths = []
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue
        if not re.fullmatch(r"\d+", path.stem):
            continue
        paths.append(path)
    return paths


def _read_legacy_entries() -> list[dict[str, Any]]:
    entries = []
    for path in _legacy_paths():
        record = _read_json(path, None)
        if isinstance(record, dict) and record:
            entries.append({"record": record, "month": None, "path": path})
    return entries


# ---------------------------------------------------------------------------
# 读取 / 归并 / 对账
# ---------------------------------------------------------------------------


def _merge_group(copies: list[dict[str, Any]]) -> dict[str, Any]:
    """合并同一笔调用的多来源副本：分片内容优先，旧目录补齐缺失字段。"""
    ordered = sorted(copies, key=lambda item: 0 if item["source"] == "legacy" else 1)
    merged: dict[str, Any] = {}
    sources: list[str] = []
    shard_month: str | None = None
    legacy_month: str | None = None
    shard_path: Path | None = None
    legacy_path: Path | None = None

    for copy in ordered:
        record = copy["record"]
        for key, value in record.items():
            if value is None or value == "":
                continue
            merged[key] = value
        if copy["source"] not in sources:
            sources.append(copy["source"])
        if copy["source"] == "shard":
            shard_month = copy["month"] or record.get("_shard_month") or shard_month
            shard_path = copy["path"]
        else:
            legacy_month = month_of(record.get("_logged_at") or record.get("timestamp")) or legacy_month
            legacy_path = copy["path"]

    ts = record_ts(merged)
    logged_month = ts.strftime("%Y-%m") if ts else (shard_month or legacy_month)
    rec_no = record_no(merged)
    audit_id = origin_audit_id(merged)

    merged["record_no"] = rec_no
    merged["audit_id"] = audit_id
    merged["_sources"] = sources
    merged["_source"] = "+".join(sources) if len(sources) > 1 else (sources[0] if sources else "unknown")
    merged["_origin_record_no"] = rec_no
    merged["_origin_audit_id"] = audit_id
    merged["_shard_month"] = shard_month
    merged["_legacy_month"] = legacy_month
    # 实际写入月份以记录自身为准，其次才用分片目录月份兜底。
    merged["_written_month"] = merged.get("_written_month") or shard_month
    merged["_logged_month"] = logged_month
    merged["_timezone"] = TZ_LABEL
    merged["_has_shard"] = shard_month is not None
    merged["_has_legacy"] = legacy_month is not None or "legacy" in sources
    if shard_path is not None:
        merged["_shard_path"] = str(shard_path)
    if legacy_path is not None:
        merged["_legacy_path"] = str(legacy_path)
    return merged


def query_audit(
    api_name: str | None = None,
    caller: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    *,
    end_exclusive: bool = False,
    include_legacy: bool = True,
    include_shards: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按区间查询审计明细，返回 (去重后的记录, 对账信息)。

    区间边界按东八区解释；`end_exclusive=True` 时用于归档的“之前”语义。
    """
    start_dt = _start_bound(start_time)
    end_dt, end_inclusive = _end_bound(end_time, exclusive=end_exclusive)

    month_hint = _months_between(start_dt, end_dt) if end_dt is not None else None
    if month_hint and include_shards:
        shard_entries = _read_shard_entries(month_hint)
        # 区间起点所在月的相邻月也读一下，避免时区偏移造成边界月漏读；
        # 真正的归属过滤仍由 _in_range 按 +08:00 完成。
        all_months = set(shard_months())
        extra = set()
        for month in month_hint:
            year, mon = int(month[:4]), int(month[5:7])
            for delta in (-1, 1):
                total = year * 12 + (mon - 1) + delta
                extra.add(f"{total // 12:04d}-{total % 12 + 1:02d}")
        extra &= all_months
        seen = {(e["month"], str(e["path"]), e.get("index")) for e in shard_entries}
        for entry in _read_shard_entries(sorted(extra)):
            marker = (entry["month"], str(entry["path"]), entry.get("index"))
            if marker not in seen:
                shard_entries.append(entry)
    else:
        shard_entries = _read_shard_entries() if include_shards else []
    legacy_entries = _read_legacy_entries() if include_legacy else []

    raw: list[dict[str, Any]] = []
    legacy_raw = 0
    shard_raw = 0
    for entry in legacy_entries:
        record = entry["record"]
        if api_name and record.get("api_name") != api_name:
            continue
        if caller and record.get("caller") != caller:
            continue
        ts = record_ts(record)
        if not _in_range(ts, start_dt, end_dt, end_inclusive):
            continue
        legacy_raw += 1
        raw.append({"source": "legacy", "record": record, "month": None, "path": entry["path"]})
    for entry in shard_entries:
        record = entry["record"]
        if api_name and record.get("api_name") != api_name:
            continue
        if caller and record.get("caller") != caller:
            continue
        ts = record_ts(record)
        if not _in_range(ts, start_dt, end_dt, end_inclusive):
            continue
        shard_raw += 1
        raw.append({"source": "shard", "record": record, "month": entry["month"],
                    "path": entry["path"]})

    # 按“原始编号 / 原始 audit_id”归并，同一次请求只算一笔
    dsu = _DisjointSet()
    key_index: dict[str, int] = {}
    for index, item in enumerate(raw):
        dsu.add(index)
        for key in identity_keys(item["record"]):
            if key in key_index:
                dsu.union(key_index[key], index)
            else:
                key_index[key] = index

    # 兜底：没有编号的记录，仅当同一指纹同时出现在两个来源时，按“一对一”
    # 配对合并（分片副本对应旧目录的一份），同一来源内的重复调用仍分开计。
    fingerprints: dict[str, dict[str, list[int]]] = {}
    for index, item in enumerate(raw):
        if identity_keys(item["record"]):
            continue
        fingerprint = _content_fingerprint(item["record"])
        if fingerprint:
            fingerprints.setdefault(fingerprint, {}).setdefault(
                item["source"], []).append(index)
    for by_source in fingerprints.values():
        legacy_indexes = by_source.get("legacy", [])
        shard_indexes = by_source.get("shard", [])
        for legacy_index, shard_index in zip(legacy_indexes, shard_indexes):
            dsu.union(legacy_index, shard_index)

    groups: dict[int, list[dict[str, Any]]] = {}
    for index in range(len(raw)):
        groups.setdefault(dsu.find(index), []).append(raw[index])

    merged_records: list[dict[str, Any]] = []
    counts = {"legacy": 0, "shard": 0, "both": 0}
    month_counts: dict[str, int] = {}
    legacy_only_months: set[str] = set()
    for group in groups.values():
        merged = _merge_group(group)
        sources = set(merged["_sources"])
        if sources == {"legacy", "shard"}:
            counts["both"] += 1
        elif sources == {"legacy"}:
            counts["legacy"] += 1
            if merged.get("_logged_month"):
                legacy_only_months.add(merged["_logged_month"])
        else:
            counts["shard"] += 1
        month = merged.get("_logged_month") or "unknown"
        month_counts[month] = month_counts.get(month, 0) + 1
        merged_records.append(merged)

    merged_records.sort(key=lambda r: (
        record_ts(r) or datetime.min.replace(tzinfo=TZ),
        str(r.get("record_no") or r.get("audit_id") or ""),
    ))

    manifest = read_manifest()
    manifest_months = set(manifest["months"].keys())
    scanned_months = sorted({entry["month"] for entry in shard_entries})
    if month_hint:
        expected_months = sorted(set(month_hint))
    else:
        expected_months = sorted(set(scanned_months) | {m for m in month_counts if m != "unknown"})
    # 只有“旧目录还有未落分片的行、且该月完全没有任何分片/迁移登记”才算缺片；
    # 区间内本来就无数据的月份不算异常。
    months_with_legacy = {
        month for month in expected_months if _legacy_remaining_for(month) > 0
    }
    missing_shards = [
        month for month in expected_months
        if month in months_with_legacy
        and month not in scanned_months
        and month not in manifest_months
    ]
    # 迁移中：清单登记为 partial/migrating 的月份；若旧目录仍有未落分片的行
    # 但该月没有清单登记，也按“迁移中”处理。
    partial_months = sorted({
        month for month in expected_months
        if str(manifest["months"].get(month, {}).get("status") or "").lower()
        in {"partial", "migrating"}
        or (month in months_with_legacy and month not in manifest_months)
    })
    # 未迁完的旧行按实际文件现算，避免清单里的进度过期。
    legacy_remaining = sum(_legacy_remaining_for(month) for month in expected_months)

    duplicates = legacy_raw + shard_raw - len(merged_records)
    reconciliation = {
        "timezone": TZ_LABEL,
        "start_time": start_time,
        "end_time": end_time,
        "end_exclusive": end_exclusive,
        "expected_months": expected_months,
        "scanned_shard_months": scanned_months,
        "legacy_records": legacy_raw,
        "shard_records": shard_raw,
        "unique_records": len(merged_records),
        "duplicates_removed": duplicates,
        "both_sources": counts["both"],
        "legacy_only": counts["legacy"],
        "shard_only": counts["shard"],
        "legacy_only_months": sorted(legacy_only_months),
        "month_counts": dict(sorted(month_counts.items())),
        "partial_months": partial_months,
        "missing_shards": missing_shards,
        "legacy_remaining": legacy_remaining,
    }
    # balanced：去重后的笔数等于来源分类之和，没有因分片不完整丢行。
    reconciliation["balanced"] = (
        reconciliation["unique_records"]
        == counts["both"] + counts["legacy"] + counts["shard"]
    )
    reconciliation["complete"] = (
        reconciliation["balanced"]
        and not partial_months
        and not missing_shards
        and legacy_remaining == 0
    )
    return merged_records, reconciliation


def reconcile_audit_range(
    start_time: str | None = None,
    end_time: str | None = None,
    *,
    end_exclusive: bool = False,
) -> dict[str, Any]:
    _, reconciliation = query_audit(
        start_time=start_time, end_time=end_time, end_exclusive=end_exclusive)
    return reconciliation


# ---------------------------------------------------------------------------
# 单条上下行
# ---------------------------------------------------------------------------


def write_audit_record(audit_id: Any, audit_data: dict[str, Any]) -> None:
    """新增/更新一条审计明细，写入对应月份分片。

    已有 `_logged_at` 时原样保留，避免归档改写把记录挪到别的月份。
    """
    payload = dict(audit_data)
    payload["audit_id"] = audit_id
    if payload.get("_logged_at"):
        payload["_logged_at"] = str(payload["_logged_at"])
    else:
        payload["_logged_at"] = now_iso()
    if payload.get("record_no") in (None, "") and payload.get("记录编号"):
        payload["record_no"] = payload["记录编号"]
    write_shard_record(payload, month_of(payload["_logged_at"]))


def read_audit_record(audit_id: Any) -> dict[str, Any]:
    slug = _safe_slug(audit_id)
    for month in reversed(shard_months()):
        record = _read_json(shard_dir() / month / f"{slug}.json", None)
        if isinstance(record, dict) and record:
            return record
    legacy = _read_json(audit_dir() / f"{slug}.json", {})
    return legacy if isinstance(legacy, dict) else {}


def next_audit_id() -> int:
    """跨旧目录和月分片取下一个可用的原始 audit_id。"""
    highest = 0
    for entry in _read_legacy_entries() + _read_shard_entries():
        record = entry["record"]
        candidates = [record.get("audit_id")]
        rec_no = record_no(record)
        if rec_no:
            match = re.search(r"(\d+)$", str(rec_no))
            if match:
                candidates.append(match.group(1))
        for value in candidates:
            try:
                highest = max(highest, int(value))
            except (TypeError, ValueError):
                continue
    return highest + 1


def _write_back(record: dict[str, Any], patch: dict[str, Any]) -> None:
    """把状态等字段写回它所在的来源，保留原始编号与原始时间戳。

    两套来源都有的记录会同时写回，避免下次查询因为分片优先而读到旧状态。
    """
    targets: list[Path] = []
    if record.get("_shard_path"):
        targets.append(Path(record["_shard_path"]))
    if record.get("_legacy_path"):
        targets.append(Path(record["_legacy_path"]))
    if not targets:
        shard_month = record.get("_shard_month") or month_of(record.get("_logged_at"))
        slug = _safe_slug(record.get("audit_id") or record.get("record_no"))
        if shard_month:
            targets.append(shard_dir() / shard_month / f"{slug}.json")
        if record.get("audit_id") is not None:
            targets.append(audit_dir() / f"{_safe_slug(record.get('audit_id'))}.json")

    for target in targets:
        if not target.exists():
            continue
        stored = _read_json(target, {}) or {}
        stored.update(patch)
        stored["record_no"] = stored.get("record_no") or record.get("record_no")
        stored["_logged_at"] = stored.get("_logged_at") or record.get("_logged_at")
        _write_json(target, stored)


# ---------------------------------------------------------------------------
# 归档：按原日期切历史明细
# ---------------------------------------------------------------------------


def archive_audit_records(before_date: str) -> dict[str, Any]:
    """归档 `before_date`（东八区自然日）之前的审计明细。

    查询同时覆盖旧目录和月分片，按原始编号去重；归档结果保留：
    原编号、原时间戳、归属月份、实际写入月份和来源。
    """
    records, reconciliation = query_audit(end_time=before_date, end_exclusive=True)
    cutoff = parse_ts(before_date)
    if cutoff is not None and len(str(before_date).strip()) <= 10:
        cutoff = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)

    archived: list[dict[str, Any]] = []
    for record in records:
        ts = record_ts(record)
        if cutoff is not None and (ts is None or ts >= cutoff):
            continue
        archived.append(_archive_row(record))

    month_counts: dict[str, int] = {}
    source_counts = {"legacy": 0, "shard": 0, "both": 0}
    for row in archived:
        month_counts[row["archive_month"]] = month_counts.get(row["archive_month"], 0) + 1
        source = row["source"]
        if source == "legacy+shard":
            source_counts["both"] += 1
        elif source in source_counts:
            source_counts[source] += 1

    archive_data = {
        "archive_date": now_iso(),
        "before_date": before_date,
        "timezone": TZ_LABEL,
        "archived_count": len(archived),
        "unique_count": len(archived),
        "duplicates_removed": reconciliation.get("duplicates_removed", 0),
        "source_counts": source_counts,
        "month_counts": dict(sorted(month_counts.items())),
        "reconciliation": reconciliation,
        "records": archived,
    }

    legacy_archive = audit_dir() / f"_archive_{before_date}.json"
    archive_data["archive_file"] = str(legacy_archive)
    _write_json(legacy_archive, archive_data)

    # 按原日期切出的历史明细，再按归属月份分文件，便于跨月对账。
    if archived:
        base = audit_dir() / ARCHIVE_DIRNAME / before_date
        for month, count in sorted(month_counts.items()):
            month_rows = [row for row in archived if row["archive_month"] == month]
            _write_json(base / f"{month}.json", {
                "month": month,
                "before_date": before_date,
                "timezone": TZ_LABEL,
                "count": count,
                "records": month_rows,
            })
        _write_json(base / "index.json", {
            "before_date": before_date,
            "timezone": TZ_LABEL,
            "archived_count": len(archived),
            "month_counts": dict(sorted(month_counts.items())),
            "source_counts": source_counts,
        })

    for record in records:
        ts = record_ts(record)
        if cutoff is not None and (ts is None or ts >= cutoff):
            continue
        _write_back(record, {
            "status": "已归档",
            "archive_before_date": before_date,
            "archived_at": archive_data["archive_date"],
            "archive_month": record.get("_logged_month"),
        })
    return archive_data


def original_local_date(record: dict[str, Any]) -> str | None:
    """原始时间戳按它自带时区得到的日期，用于说明“原日期”口径。"""
    raw = record.get("timestamp") or record.get("时间戳")
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%d")


def _archive_row(record: dict[str, Any]) -> dict[str, Any]:
    row = dict(record)
    row["origin_record_no"] = record.get("_origin_record_no") or record_no(record)
    row["origin_audit_id"] = record.get("_origin_audit_id") or origin_audit_id(record)
    row["original_timestamp"] = record.get("timestamp") or record.get("时间戳")
    row["original_date"] = original_local_date(record)
    row["logged_at"] = record.get("_logged_at")
    # 归档月份按东八区边界切：这是“原日期”在本平台的统一口径。
    row["archive_month"] = record.get("_logged_month")
    row["written_month"] = record.get("_written_month")
    row["legacy_month"] = record.get("_legacy_month")
    row["source"] = record.get("_source")
    return row


# ---------------------------------------------------------------------------
# 存量迁移流水线
# ---------------------------------------------------------------------------


def migrate_legacy_audit(
    months: Iterable[str] | None = None,
    *,
    remove_legacy: bool = False,
    per_month_limit: int | None = None,
) -> dict[str, Any]:
    """把旧目录的逐条 JSON 迁入月分片。

    可重复执行；已存在的同一笔调用会跳过，不会产生重复记录。
    `per_month_limit` 用于分批迁移，未搬完的月份标记为 partial，
    查询侧仍会把旧目录里的剩余记录并进来。
    """
    wanted = set(months) if months else None
    legacy_entries = _read_legacy_entries()
    by_month: dict[str, list[dict[str, Any]]] = {}
    unknown = []
    for entry in legacy_entries:
        month = month_of(entry["record"].get("_logged_at") or entry["record"].get("timestamp"))
        if month is None:
            unknown.append(entry)
            continue
        if wanted is not None and month not in wanted:
            continue
        by_month.setdefault(month, []).append(entry)

    summary: dict[str, Any] = {
        "timezone": TZ_LABEL,
        "copied": 0,
        "skipped": 0,
        "removed_legacy": 0,
        "unknown_month": len(unknown),
        "months": {},
    }

    for month in sorted(by_month):
        existing = _read_shard_entries([month])
        existing_keys: set[str] = set()
        for entry in existing:
            existing_keys.update(identity_keys(entry["record"]))
        copied = skipped = 0
        for entry in by_month[month]:
            record = entry["record"]
            keys = set(identity_keys(record))
            if keys & existing_keys:
                skipped += 1
                continue
            if per_month_limit is not None and copied >= per_month_limit:
                break
            write_shard_record(record, month)
            existing_keys.update(keys)
            copied += 1
            summary["copied"] += 1
            if remove_legacy:
                try:
                    entry["path"].unlink()
                    summary["removed_legacy"] += 1
                except OSError:
                    pass
        summary["skipped"] += skipped
        remaining = len(by_month[month]) - copied - skipped
        status = "complete" if remaining <= 0 else "partial"
        update_month_manifest(
            month,
            status=status,
            record_count=_count_shard_month(month),
            migrated_at=now_iso(),
            legacy_remaining=max(remaining, 0),
            legacy_total=len(by_month[month]),
            migration_source="legacy-json",
        )
        summary["months"][month] = {
            "copied": copied,
            "skipped": skipped,
            "remaining": max(remaining, 0),
            "status": status,
        }
    return summary


# ---------------------------------------------------------------------------
# 边界样本：迁移前月末 / 刚跨月 / 切换过程中新增
# ---------------------------------------------------------------------------

SAMPLE_RECORD_NOS = ("CALL-SAMPLE-0001", "CALL-SAMPLE-0002", "CALL-SAMPLE-0003")


def _sample_payload(record_no: str, audit_id: int, timestamp: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "audit_id": audit_id,
        "record_no": record_no,
        "记录编号": record_no,
        "caller": extra.pop("caller", "样本调用方"),
        "api_name": extra.pop("api_name", "样本API"),
        "request_params": "page=1",
        "response_code": "200",
        "latency_ms": 42.0,
        "result_rows": 10,
        "timestamp": timestamp,
        "_logged_at": timestamp,
        "status": "已记录",
    }
    payload.update(extra)
    return payload


def _legacy_remaining_for(month: str) -> int:
    """旧目录里属于该月、但分片里还没有的行数。"""
    shard_keys: set[str] = set()
    for entry in _read_shard_entries([month]):
        shard_keys.update(identity_keys(entry["record"]))
    remaining = 0
    for entry in _read_legacy_entries():
        record = entry["record"]
        if month_of(record.get("_logged_at") or record.get("timestamp")) != month:
            continue
        if set(identity_keys(record)) & shard_keys:
            continue
        remaining += 1
    return remaining


def seed_boundary_samples() -> dict[str, Any]:
    """幂等写入三个边界样本。

    1. CALL-SAMPLE-0001：迁移前月末（2026-10-31 23:30 +08:00），只在旧目录。
    2. CALL-SAMPLE-0002：刚跨月（原始时间戳 2026-10-31T16:20+00:00，按东八区
       归属 2026-11-01 00:20），旧目录与月分片各有一份，查询只算一笔。
    3. CALL-SAMPLE-0003：切换过程中新增（2026-11-01 09:05 +08:00），只在新分片。
    """
    existing_ids = {
        record_no(entry["record"]) for entry in _read_legacy_entries() + _read_shard_entries()
    }
    created: list[str] = []
    base_id = next_audit_id()

    samples = [
        # (record_no, 原始时间戳, 写旧目录, 写月分片, 附加字段)
        ("CALL-SAMPLE-0001", "2026-10-31T23:30:00+08:00", True, False,
         {"_record_origin": "legacy-before-migration"}),
        ("CALL-SAMPLE-0002", "2026-10-31T16:20:00+00:00", True, True,
         {"_record_origin": "cutover-overlap"}),
        ("CALL-SAMPLE-0003", "2026-11-01T09:05:00+08:00", False, True,
         {"_record_origin": "new-pipeline"}),
    ]
    for offset, (rec_no, ts, to_legacy, to_shard, extra) in enumerate(samples):
        if rec_no in existing_ids:
            continue
        audit_id = base_id + offset
        payload = _sample_payload(rec_no, audit_id, ts, **extra)
        if to_legacy:
            _write_json(audit_dir() / f"{audit_id:06d}.json", payload)
        if to_shard:
            write_shard_record(payload, month_of(ts))
        existing_ids.add(rec_no)
        created.append(rec_no)

    # 十月处于“迁移中”：分片只转了一部分，旧目录仍有剩余行。
    update_month_manifest(
        "2026-10",
        status="partial",
        record_count=_count_shard_month("2026-10"),
        legacy_remaining=_legacy_remaining_for("2026-10"),
        migration_source="legacy-json",
    )
    update_month_manifest(
        "2026-11",
        status="active",
        record_count=_count_shard_month("2026-11"),
        legacy_remaining=_legacy_remaining_for("2026-11"),
    )
    return {"created": created, "samples": list(SAMPLE_RECORD_NOS)}


def boundary_sample_expectations() -> list[dict[str, Any]]:
    return [
        {
            "record_no": "CALL-SAMPLE-0001",
            "scene": "迁移前月末",
            "original_timestamp": "2026-10-31T23:30:00+08:00",
            "original_date": "2026-10-31",
            "expected_month": "2026-10",
            "expected_sources": ["legacy"],
            "expected_written_month": None,
            "in_archive_before": "2026-11-01",
            "in_archive": True,
        },
        {
            "record_no": "CALL-SAMPLE-0002",
            "scene": "刚跨月（原始偏移为 UTC，东八区归属 11 月）",
            "original_timestamp": "2026-10-31T16:20:00+00:00",
            "original_date": "2026-10-31",
            "expected_month": "2026-11",
            "expected_sources": ["legacy", "shard"],
            "expected_written_month": "2026-11",
            "in_archive_before": "2026-11-01",
            "in_archive": False,
            "note": "边界按 +08:00 换算，归属 11 月；同一次调用在两套来源各有一份，只算一笔。",
        },
        {
            "record_no": "CALL-SAMPLE-0003",
            "scene": "切换过程中新增",
            "original_timestamp": "2026-11-01T09:05:00+08:00",
            "original_date": "2026-11-01",
            "expected_month": "2026-11",
            "expected_sources": ["shard"],
            "expected_written_month": "2026-11",
            "in_archive_before": "2026-11-01",
            "in_archive": False,
        },
    ]


# ---------------------------------------------------------------------------
# 分片清单视图
# ---------------------------------------------------------------------------


def shard_overview() -> dict[str, Any]:
    manifest = read_manifest()
    legacy_entries = _read_legacy_entries()
    legacy_by_month: dict[str, int] = {}
    for entry in legacy_entries:
        month = month_of(entry["record"].get("_logged_at") or entry["record"].get("timestamp"))
        if month:
            legacy_by_month[month] = legacy_by_month.get(month, 0) + 1
    months = []
    for month in shard_months():
        info = dict(manifest["months"].get(month) or {})
        info["month"] = month
        info.setdefault("status", "active")
        info["record_count"] = _count_shard_month(month)
        info["legacy_records"] = legacy_by_month.get(month, 0)
        months.append(info)
    return {
        "timezone": TZ_LABEL,
        "manifest": manifest,
        "months": months,
        "legacy_files": len(legacy_entries),
        "legacy_by_month": dict(sorted(legacy_by_month.items())),
    }
