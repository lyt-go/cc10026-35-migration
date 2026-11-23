/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type AuditReconciliation = {
  timezone: string
  start_time: string | null
  end_time: string | null
  end_exclusive: boolean
  expected_months: string[]
  scanned_shard_months: string[]
  legacy_records: number
  shard_records: number
  unique_records: number
  duplicates_removed: number
  both_sources: number
  legacy_only: number
  shard_only: number
  legacy_only_months: string[]
  month_counts: Record<string, number>
  partial_months: string[]
  missing_shards: string[]
  legacy_remaining: number
  balanced: boolean
  complete: boolean
}

export type AuditRow = EntryRow & {
  原始编号?: string
  数据来源?: string
  归属月份?: string | null
  写入月份?: string | null
  原始时间戳?: string | null
  时区?: string
}

export type AuditRangeResult = {
  items: AuditRow[]
  reconciliation: AuditReconciliation
  timezone: string
}

export type AuditArchivePreview = {
  before_date: string
  timezone: string
  items: AuditRow[]
  month_counts: Record<string, number>
  reconciliation: AuditReconciliation
}

export type AuditSampleExpectation = {
  record_no: string
  scene: string
  original_timestamp: string
  original_date: string
  expected_month: string
  expected_sources: string[]
  expected_written_month: string | null
  in_archive_before: string
  in_archive: boolean
  note?: string
}

export type AuditBoundarySamples = {
  timezone: string
  samples: AuditSampleExpectation[]
  view: { items: AuditRow[]; reconciliation: AuditReconciliation }
  archive: {
    before_date: string
    items: AuditRow[]
    month_counts: Record<string, number>
    reconciliation: AuditReconciliation
  }
}

export type AuditShardInfo = {
  month: string
  status: string
  record_count: number
  legacy_records: number
  legacy_remaining?: number
  migration_source?: string
}

export type AuditShardOverview = {
  timezone: string
  months: AuditShardInfo[]
  legacy_files: number
  legacy_by_month: Record<string, number>
}

export type AuditMigrationResult = {
  timezone: string
  copied: number
  skipped: number
  removed_legacy: number
  unknown_month: number
  months: Record<
    string,
    { copied: number; skipped: number; remaining: number; status: string }
  >
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}
