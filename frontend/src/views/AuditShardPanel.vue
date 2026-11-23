<template>
  <section class="audit-shard" data-testid="audit-shard-panel">
    <header class="shard-head">
      <div>
        <h3>按月分片审计对账</h3>
        <p class="page-desc">
          查看与归档都读取「旧逐条例 + 月分片」并集，按原始编号去重；月份边界统一为东八区（{{ timezone }}）。
        </p>
      </div>
      <button class="btn ghost" data-testid="audit-refresh-shards" type="button" @click="refreshShards">
        刷新分片清单
      </button>
    </header>

    <div class="shard-filters">
      <label>
        <span>开始日期</span>
        <input v-model="startDate" data-testid="audit-start" type="date" />
      </label>
      <label>
        <span>结束日期</span>
        <input v-model="endDate" data-testid="audit-end" type="date" />
      </label>
      <label>
        <span>调用方</span>
        <input v-model="caller" placeholder="可选" />
      </label>
      <label>
        <span>目标API</span>
        <input v-model="apiName" placeholder="可选" />
      </label>
      <button class="btn primary" data-testid="audit-query" type="button" @click="runView">
        区间查看
      </button>
      <button class="btn" data-testid="audit-preview" type="button" @click="runPreview">
        归档预览
      </button>
      <button class="btn" data-testid="audit-archive" type="button" @click="runArchive">
        执行归档（{{ beforeDate }} 之前）
      </button>
      <button class="btn" data-testid="audit-samples" type="button" @click="runSamples">
        核对三个边界样本
      </button>
    </div>

    <p v-if="message" class="shard-message" data-testid="audit-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="audit-error">{{ errorMessage }}</p>

    <div class="shard-months">
      <article v-for="shard in shards.months" :key="shard.month" class="shard-chip"
               :data-testid="`audit-shard-${shard.month}`"
               :data-status="shard.status">
        <strong>{{ shard.month }}</strong>
        <span>{{ shard.status === 'partial' ? '迁移中' : shard.status === 'complete' ? '已完成' : shard.status }}</span>
        <span>分片 {{ shard.record_count }} 条</span>
        <span v-if="shard.legacy_records">旧目录 {{ shard.legacy_records }} 条</span>
      </article>
      <article v-if="!shards.months.length" class="shard-chip">暂无月分片</article>
      <article class="shard-chip">旧目录文件 {{ shards.legacy_files }} 个</article>
    </div>

    <section v-if="reconciliation" class="shard-recon" data-testid="audit-reconciliation">
      <div class="recon-cards">
        <article><span class="stat-label">旧目录行</span><strong>{{ reconciliation.legacy_records }}</strong></article>
        <article><span class="stat-label">分片行</span><strong>{{ reconciliation.shard_records }}</strong></article>
        <article><span class="stat-label">去重后</span><strong>{{ reconciliation.unique_records }}</strong></article>
        <article><span class="stat-label">去重掉</span><strong>{{ reconciliation.duplicates_removed }}</strong></article>
        <article><span class="stat-label">双来源</span><strong>{{ reconciliation.both_sources }}</strong></article>
        <article><span class="stat-label">仅在旧目录</span><strong>{{ reconciliation.legacy_only }}</strong></article>
        <article><span class="stat-label">仅在新分片</span><strong>{{ reconciliation.shard_only }}</strong></article>
      </div>
      <p class="recon-line">
        归属月份：<span v-for="(count, month) in reconciliation.month_counts" :key="month" class="legend-item">
          {{ month }} = {{ count }}
        </span>
      </p>
      <p class="recon-line">
        <span :class="reconciliation.balanced ? 'badge-ok' : 'badge-bad'">
          {{ reconciliation.balanced ? '对账平衡' : '对账不平衡' }}
        </span>
        <span :class="reconciliation.complete ? 'badge-ok' : 'badge-warn'">
          {{ reconciliation.complete ? '迁移完整' : '尚有未迁完分片' }}
        </span>
        <span v-if="reconciliation.partial_months.length" class="badge-warn">
          迁移中：{{ reconciliation.partial_months.join('、') }}
        </span>
        <span v-if="reconciliation.legacy_remaining" class="badge-warn">
          旧目录待迁 {{ reconciliation.legacy_remaining }} 行
        </span>
      </p>
    </section>

    <table v-if="rows.length" class="data-table" data-testid="audit-range-table">
      <thead>
        <tr>
          <th>原始编号</th>
          <th>原始时间戳</th>
          <th>归属月份</th>
          <th>写入月份</th>
          <th>来源</th>
          <th>调用方</th>
          <th>目标API</th>
          <th>状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="`${row.原始编号}-${row.id}`" :data-testid="`audit-row-${row.原始编号}`">
          <td>{{ row.原始编号 ?? row['记录编号'] }}</td>
          <td>{{ row.原始时间戳 ?? row['时间戳'] }}</td>
          <td>{{ row.归属月份 ?? '—' }}</td>
          <td>{{ row.写入月份 ?? '—' }}</td>
          <td>{{ sourceLabel(row.数据来源) }}</td>
          <td>{{ row['调用方'] }}</td>
          <td>{{ row['目标API'] }}</td>
          <td>{{ row.status }}</td>
        </tr>
      </tbody>
    </table>

    <section v-if="samples" class="shard-samples" data-testid="audit-samples-result">
      <h4>边界样本核对</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>场景</th>
            <th>原始编号</th>
            <th>原始时间戳</th>
            <th>预期归属</th>
            <th>查看入口</th>
            <th>归档入口</th>
            <th>结论</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in sampleRows" :key="item.record_no" :data-testid="`audit-sample-${item.record_no}`">
            <td>{{ item.scene }}</td>
            <td>{{ item.record_no }}</td>
            <td>{{ item.original_timestamp }}</td>
            <td>{{ item.expected_month }}</td>
            <td>{{ describe(item.view) }}</td>
            <td>{{ describe(item.archive) }}</td>
            <td :class="item.pass ? 'badge-ok' : 'badge-bad'">{{ item.pass ? '一致' : '不一致' }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  archiveAudit,
  loadAuditSamples,
  loadAuditShards,
  previewAuditArchive,
  queryAuditRange,
} from '@/api/backend-service'
import type {
  AuditBoundarySamples,
  AuditReconciliation,
  AuditRow,
  AuditShardOverview,
} from '@/data/types'

const startDate = ref('2026-10-01')
const endDate = ref('2026-11-30')
const beforeDate = ref('2026-11-01')
const caller = ref('')
const apiName = ref('')

const rows = ref<AuditRow[]>([])
const reconciliation = ref<AuditReconciliation | null>(null)
const shards = ref<AuditShardOverview>({
  timezone: '+08:00',
  months: [],
  legacy_files: 0,
  legacy_by_month: {},
})
const samples = ref<AuditBoundarySamples | null>(null)
const message = ref('')
const errorMessage = ref('')
const timezone = computed(() => reconciliation.value?.timezone ?? shards.value.timezone)

type SampleView = {
  scene: string
  record_no: string
  original_timestamp: string
  expected_month: string
  view?: AuditRow
  archive?: AuditRow
  pass: boolean
}

const sampleRows = computed<SampleView[]>(() => {
  if (!samples.value) return []
  return samples.value.samples.map((expect) => {
    const view = samples.value?.view.items.find((item) => item.原始编号 === expect.record_no)
    const archived = samples.value?.archive.items.find((item) => item.原始编号 === expect.record_no)
    const sources = (view?.数据来源 ?? '').split('+').filter(Boolean).sort()
    const expectedSources = [...expect.expected_sources].sort()
    const pass =
      !!view &&
      view.归属月份 === expect.expected_month &&
      (view.写入月份 ?? null) === (expect.expected_written_month ?? null) &&
      sources.join('+') === expectedSources.join('+') &&
      !!archived === expect.in_archive
    return {
      scene: expect.scene,
      record_no: expect.record_no,
      original_timestamp: expect.original_timestamp,
      expected_month: expect.expected_month,
      view,
      archive: archived,
      pass,
    }
  })
})

function describe(row?: AuditRow): string {
  if (!row) return '未命中'
  return `${row.归属月份 ?? '—'} / ${row.数据来源 ?? '未知'}${row.写入月份 ? ` / 写入 ${row.写入月份}` : ''}`
}

function sourceLabel(source?: string): string {
  if (source === 'legacy') return '旧目录'
  if (source === 'shard') return '月分片'
  if (source === 'legacy+shard') return '两套来源（已去重）'
  return source || '内存样例'
}

function reset() {
  message.value = ''
  errorMessage.value = ''
}

async function refreshShards() {
  try {
    shards.value = await loadAuditShards()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分片清单读取失败'
  }
}

async function runView() {
  reset()
  try {
    const result = await queryAuditRange({
      startTime: startDate.value,
      endTime: endDate.value,
      caller: caller.value || undefined,
      apiName: apiName.value || undefined,
    })
    rows.value = result.items
    reconciliation.value = result.reconciliation
    message.value = `区间查看：去重后 ${result.reconciliation.unique_records} 笔`
    await refreshShards()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '审计区间查询失败'
  }
}

async function runPreview() {
  reset()
  try {
    const preview = await previewAuditArchive(beforeDate.value)
    rows.value = preview.items
    reconciliation.value = preview.reconciliation
    message.value = `归档预览：${beforeDate.value} 之前共 ${preview.items.length} 笔`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归档预览失败'
  }
}

async function runArchive() {
  reset()
  try {
    const result = await archiveAudit(beforeDate.value)
    await runView()
    message.value = result.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归档失败'
  }
}

async function runSamples() {
  reset()
  try {
    samples.value = await loadAuditSamples()
    reconciliation.value = samples.value.view.reconciliation
    rows.value = samples.value.view.items
    message.value = '三个边界样本已核对：迁移前月末 / 刚跨月 / 切换过程中新增'
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '边界样本核对失败'
  }
}

onMounted(async () => {
  await refreshShards()
  await runView()
})
</script>

<style scoped>
.audit-shard {
  margin: 16px 0;
  padding: 14px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.shard-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.shard-head h3 { margin: 0; font-size: 15px; }
.shard-filters { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin: 10px 0; }
.shard-filters label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.shard-filters input { padding: 5px 8px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.shard-message { font-size: 12px; color: #1f6feb; margin: 4px 0; }
.shard-months { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0; }
.shard-chip {
  display: flex; flex-direction: column; gap: 2px; min-width: 120px;
  padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px;
  background: #fafbfc; font-size: 12px; color: var(--muted);
}
.shard-chip strong { color: #1f2937; }
.shard-chip[data-status='partial'] { border-color: #f59e0b; background: #fffbeb; }
.shard-recon { margin: 10px 0; }
.recon-cards { display: flex; flex-wrap: wrap; gap: 8px; }
.recon-cards article {
  flex: 1; min-width: 96px; padding: 8px 10px;
  border: 1px solid var(--border); border-radius: 6px; background: #fafbfc;
}
.recon-cards strong { font-size: 18px; }
.recon-line { font-size: 12px; color: var(--muted); margin: 6px 0; display: flex; flex-wrap: wrap; gap: 8px; }
.badge-ok { color: #15803d; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 4px; padding: 0 6px; }
.badge-warn { color: #b45309; background: #fffbeb; border: 1px solid #fde68a; border-radius: 4px; padding: 0 6px; }
.badge-bad { color: #b42318; background: #fef3f2; border: 1px solid #fecdca; border-radius: 4px; padding: 0 6px; }
.shard-samples h4 { margin: 14px 0 6px; font-size: 14px; }
</style>
