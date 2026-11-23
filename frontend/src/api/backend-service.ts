import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  AuditArchivePreview,
  AuditBoundarySamples,
  AuditMigrationResult,
  AuditRangeResult,
  AuditShardOverview,
  EntryRow,
  OverviewResult,
  PageResult,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---- 调用审计分片：区间查看、归档、迁移 ----

function auditQuery(params: Record<string, string | number | boolean | undefined>): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === '') continue
    query.set(key, String(value))
  }
  return query.toString()
}

export async function queryAuditRange(params: {
  startTime?: string
  endTime?: string
  apiName?: string
  caller?: string
  endExclusive?: boolean
}): Promise<AuditRangeResult> {
  const query = auditQuery({
    start_time: params.startTime,
    end_time: params.endTime,
    api_name: params.apiName,
    caller: params.caller,
    end_exclusive: params.endExclusive,
  })
  const response = await request(`/api/audit/records?${query}`)
  if (!response.ok) throw new Error('审计区间查询失败')
  return await response.json()
}

export async function previewAuditArchive(beforeDate: string): Promise<AuditArchivePreview> {
  const response = await request(
    `/api/audit/archive/preview?${auditQuery({ before_date: beforeDate })}`,
  )
  if (!response.ok) throw new Error('归档预览失败')
  return await response.json()
}

export async function archiveAudit(beforeDate: string): Promise<ActionResult> {
  const response = await request(`/api/audit/archive?${auditQuery({ before_date: beforeDate })}`, {
    method: 'POST',
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload?.detail ?? '归档失败')
  const months = Object.entries(payload.month_counts ?? {})
    .map(([month, count]) => `${month} ${count} 条`)
    .join('、')
  return {
    ok: payload.ok !== false,
    message: `已归档 ${payload.archived_count ?? 0} 笔${months ? `（${months}）` : ''}`,
  }
}

export async function loadAuditSamples(): Promise<AuditBoundarySamples> {
  const response = await request('/api/audit/samples', { method: 'POST' })
  if (!response.ok) throw new Error('边界样本核对失败')
  const payload = await response.json()
  return payload.entry as AuditBoundarySamples
}

export async function loadAuditShards(): Promise<AuditShardOverview> {
  const response = await request('/api/audit/shards')
  if (!response.ok) throw new Error('分片清单读取失败')
  return await response.json()
}

export async function migrateAuditLegacy(params: {
  months?: string
  perMonthLimit?: number
}): Promise<AuditMigrationResult> {
  const query = auditQuery({ months: params.months, per_month_limit: params.perMonthLimit })
  const response = await request(`/api/audit/migrate?${query}`, { method: 'POST' })
  if (!response.ok) throw new Error('迁移失败')
  return await response.json()
}
