<script setup lang="ts">
/**
 * P11 审计日志（F-802，对照原型 AuditView + ui-spec §八）：筛选行（操作人ID/
 * 动作/对象类型/日期范围 + 查询/重置 §22.1——后端为精确匹配契约，原型单关键字
 * LIKE 无对应查询能力，deviations #45）+ 列表（时间/操作人/动作/对象/IP/详情
 * 展开行）+ 详情 JSON 格式化只读展示（detail 为 JSON 字符串，解析后展示，失败
 * 原样——T2 评审移交契约）+ 导出 CSV（fetch blob + a[download] 触发，X-Truncated
 * 提示"已截断"；端点在主分支 68783f9 未合入，合并即生效，deviations 同 #22）。
 */
import { onMounted, reactive, ref } from 'vue'
import {
  exportAuditLogs,
  formatAuditDetail,
  listAuditLogs,
  type AuditLogItem,
} from '@/api/auditLogs'
import { ApiClientError } from '@/api/client'
import { checkDateRange } from '@/validators'

const filters = reactive({
  actorId: undefined as number | undefined,
  action: '',
  objectType: '',
  /** el-date-picker value-format YYYY-MM-DD，[from, to] 或 null */
  dateRange: null as [string, string] | null,
})
const filterErr = ref('')
const actionErr = ref('')
const items = ref<AuditLogItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const exporting = ref(false)

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await listAuditLogs({
      actorId: filters.actorId,
      action: filters.action,
      objectType: filters.objectType,
      dateFrom: filters.dateRange?.[0],
      dateTo: filters.dateRange?.[1],
      page: page.value,
      pageSize: pageSize.value,
    })
    items.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

onMounted(() => void load())

function search(): void {
  const [dateFrom, dateTo] = filters.dateRange ?? []
  const rangeCheck = checkDateRange(dateFrom, dateTo)
  if (rangeCheck !== true) {
    filterErr.value = rangeCheck
    return
  }
  filterErr.value = ''
  page.value = 1
  void load()
}

function resetFilters(): void {
  filters.actorId = undefined
  filters.action = ''
  filters.objectType = ''
  filters.dateRange = null
  search()
}

function onPageChange(next: number): void {
  page.value = next
  void load()
}

function onSizeChange(size: number): void {
  pageSize.value = size
  page.value = 1
  void load()
}

/**
 * 导出 CSV（F-802/ADM-09）：当前筛选口径与列表一致；blob → a[download] 触发；
 * X-Truncated: true 提示行数超上限已截断（内存放大保护，行数上限为服务端配置）。
 */
async function exportCsv(): Promise<void> {
  if (exporting.value) return
  const [dateFrom, dateTo] = filters.dateRange ?? []
  const rangeCheck = checkDateRange(dateFrom, dateTo)
  if (rangeCheck !== true) {
    filterErr.value = rangeCheck
    return
  }
  actionErr.value = ''
  exporting.value = true
  try {
    const result = await exportAuditLogs({
      actorId: filters.actorId,
      action: filters.action,
      objectType: filters.objectType,
      dateFrom,
      dateTo,
    })
    const url = URL.createObjectURL(result.blob)
    const a = document.createElement('a')
    a.href = url
    a.download = result.filename
    a.click()
    URL.revokeObjectURL(url)
    if (result.truncated) {
      actionErr.value = '结果行数超出导出上限，文件已截断（仅含最近记录）'
    }
  } catch (e) {
    actionErr.value = e instanceof ApiClientError ? e.message : '系统繁忙，请稍后重试'
  } finally {
    exporting.value = false
  }
}

/** 对象列：objectType #objectId；objectId 为 null 出 "-"（不造值） */
function objectText(row: AuditLogItem): string {
  return row.objectId === null ? `${row.objectType} -` : `${row.objectType} #${row.objectId}`
}

/** DTO createdAt 为 ISO 字符串 → 原型口径 "YYYY-MM-DD HH:mm" */
function formatTime(iso: string): string {
  return iso.replace('T', ' ').slice(0, 16)
}
</script>

<template>
  <div>
    <div class="filter-row">
      <el-input-number
        v-model="filters.actorId"
        :min="1"
        :controls="false"
        placeholder="操作人ID"
        style="width: 120px"
      />
      <el-input
        v-model="filters.action"
        placeholder="动作（精确匹配，如 user.create）"
        style="width: 220px"
        clearable
        @keyup.enter="search"
      />
      <el-input
        v-model="filters.objectType"
        placeholder="对象类型（如 qa_pair）"
        style="width: 170px"
        clearable
        @keyup.enter="search"
      />
      <el-date-picker
        v-model="filters.dateRange"
        type="daterange"
        value-format="YYYY-MM-DD"
        start-placeholder="开始日期"
        end-placeholder="结束日期"
        style="width: 240px"
      />
      <el-button type="primary" @click="search">查询</el-button>
      <el-button @click="resetFilters">重置</el-button>
      <span class="spacer"></span>
      <el-button :loading="exporting" data-test="export-btn" @click="exportCsv">导出 CSV</el-button>
    </div>
    <div v-if="filterErr" class="err-inline" role="alert">{{ filterErr }}</div>
    <el-alert
      v-if="actionErr"
      :title="actionErr"
      type="warning"
      role="alert"
      show-icon
      :closable="true"
      class="action-err"
      data-test="export-note"
      @close="actionErr = ''"
    />

    <el-table v-loading="loading" :data="items" border stripe row-key="id">
      <el-table-column type="expand">
        <template #default="{ row }">
          <pre class="detail-json" data-test="detail-json">{{ formatAuditDetail(row.detail) }}</pre>
        </template>
      </el-table-column>
      <el-table-column label="时间" width="150">
        <template #default="{ row }">{{ formatTime(row.createdAt) }}</template>
      </el-table-column>
      <el-table-column prop="actorUsername" label="操作人" width="120" />
      <el-table-column label="动作" width="180">
        <template #default="{ row }"
          ><code>{{ row.action }}</code></template
        >
      </el-table-column>
      <el-table-column label="对象" width="180">
        <template #default="{ row }">{{ objectText(row) }}</template>
      </el-table-column>
      <el-table-column label="IP" width="140">
        <template #default="{ row }">{{ row.ip ?? '-' }}</template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无审计记录（audit_logs 只增不删，随操作产生）" :image-size="60" />
      </template>
    </el-table>

    <div class="pager-row">
      <el-pagination
        :current-page="page"
        :page-size="pageSize"
        :page-sizes="[10, 20, 50, 100]"
        :total="total"
        layout="total, sizes, prev, pager, next"
        @current-change="onPageChange"
        @size-change="onSizeChange"
      />
    </div>
  </div>
</template>

<style scoped>
/* 对照原型 .filter-row / .pager-row（§22.1 查询条件同行、分页右下） */
.filter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.pager-row {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
.spacer {
  flex: 1;
}
.err-inline {
  color: var(--el-color-danger);
  font-size: 12px;
  margin-bottom: 8px;
}
.action-err {
  margin-bottom: 12px;
}
/* 详情 JSON 只读展示（§8.8 只读置灰语义，格式化缩进可核对） */
.detail-json {
  margin: 0;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--el-text-color-regular);
  background: var(--el-fill-color-lighter);
  border-radius: 4px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
