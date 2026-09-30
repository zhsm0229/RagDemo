<script setup lang="ts">
/**
 * P3 知识库·文档管理（ui-spec §三，RAGFlow 模式 §8.2；对照原型 DocView）：
 * 筛选行（部门/状态/关键字 + 查询/重置同行 §22.1）+ 工具栏（上传/zip 导入/手动刷新）
 * + 表格（标题含版本与降级标注/部门/状态机/责任人/生效日期/操作）+ 分页（10/20/50/100 默认 20 右下）。
 * 状态轮询（§8.4）：列表存在 parsing 行时每 30s 静默自动刷新（不闪表格 loading），
 * 组件卸载清理定时器；增删操作成功 ElMessage、失败由 client 统一 toast，上传 422/409 在弹窗内联（§22.2）。
 * 操作列无后端契约的控件（删除置灰"暂不支持"、版本历史/重建索引未渲染）见 deviations #25。
 */
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  confirmDocument,
  listDocuments,
  retryDocument,
  type DocumentItem,
  type DocumentStatus,
} from '@/api/documents'
import { DEPT_LABELS, DEPT_OPTIONS, STATUS_OPTIONS } from '@/components/documents/options'
import UploadDialog from '@/components/documents/UploadDialog.vue'
import ZipImportDialog from '@/components/documents/ZipImportDialog.vue'

/** T3（36da916）import-zip 未合入 feat/w4-fe：入口置灰不发无效调用，合并后翻开关即启用（deviations #21） */
const ZIP_IMPORT_ENABLED = false

const POLL_INTERVAL_MS = 30_000

const filters = reactive({ dept: '', status: '' as DocumentStatus | '', keyword: '' })
const items = ref<DocumentItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const uploadVisible = ref(false)
const zipVisible = ref(false)

let pollTimer: number | null = null

async function load(silent = false): Promise<void> {
  if (!silent) loading.value = true
  try {
    const data = await listDocuments({
      dept: filters.dept,
      status: filters.status,
      keyword: filters.keyword,
      page: page.value,
      pageSize: pageSize.value,
    })
    items.value = data.items
    total.value = data.total
  } finally {
    if (!silent) loading.value = false
  }
  syncPolling()
}

/** parsing 行存在才维持 30s 轮询；全部到终态即停。轮询走 silent：后台刷新不闪 loading（M-6） */
function syncPolling(): void {
  const hasParsing = items.value.some((doc) => doc.status === 'parsing')
  if (hasParsing && pollTimer === null) {
    pollTimer = window.setInterval(() => void load(true), POLL_INTERVAL_MS)
  } else if (!hasParsing && pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

function refresh(): void {
  void load()
}

onMounted(() => void load())
onUnmounted(() => {
  if (pollTimer !== null) window.clearInterval(pollTimer)
  pollTimer = null
})

function search(): void {
  page.value = 1
  void load()
}

function resetFilters(): void {
  filters.dept = ''
  filters.status = ''
  filters.keyword = ''
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

function deptName(code: string): string {
  return DEPT_LABELS[code] ?? code
}

async function retry(row: DocumentItem): Promise<void> {
  await retryDocument(row.id)
  ElMessage.success('重试已提交，文档重新进入解析队列')
  void load()
}

async function confirmRow(row: DocumentItem): Promise<void> {
  await confirmDocument(row.id)
  ElMessage.success('文档已确认激活，参与检索')
  void load()
}

function onUploaded(): void {
  uploadVisible.value = false
  page.value = 1
  void load()
}

function onZipImported(): void {
  void load()
}
</script>

<template>
  <div>
    <div class="filter-row">
      <el-select
        v-model="filters.dept"
        placeholder="部门"
        clearable
        style="width: 130px"
        @change="search"
      >
        <el-option v-for="o in DEPT_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <el-select
        v-model="filters.status"
        placeholder="状态"
        clearable
        style="width: 130px"
        @change="search"
      >
        <el-option v-for="o in STATUS_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <el-input
        v-model="filters.keyword"
        placeholder="标题关键字"
        style="width: 200px"
        clearable
        @keyup.enter="search"
        @clear="search"
      />
      <el-button type="primary" @click="search">查询</el-button>
      <el-button @click="resetFilters">重置</el-button>
      <span class="spacer"></span>
      <el-button :loading="loading" @click="refresh">刷新</el-button>
      <el-tooltip content="后端任务未就绪" placement="top">
        <span class="tip-host">
          <el-button :disabled="!ZIP_IMPORT_ENABLED" @click="zipVisible = true"
            >zip 批量导入</el-button
          >
        </span>
      </el-tooltip>
      <el-button type="primary" @click="uploadVisible = true">＋ 上传文档</el-button>
    </div>

    <el-table v-loading="loading" :data="items" border stripe>
      <el-table-column label="标题" min-width="200">
        <template #default="{ row }">
          {{ row.title }} <span class="ver">v{{ row.version }}</span>
          <el-tag v-if="row.degraded" size="small" type="info" class="gap-tag">降级解析</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="部门" width="90">
        <template #default="{ row }">{{ deptName(row.deptCode) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="200">
        <template #default="{ row }">
          <el-tag v-if="row.status === 'ready'" type="success">● 就绪</el-tag>
          <span v-else-if="row.status === 'parsing'" class="parsing-cell">
            <el-tag type="warning">◐ 解析中</el-tag>
            <el-progress
              class="bar"
              :percentage="50"
              :indeterminate="true"
              :show-text="false"
              :stroke-width="6"
              :duration="3"
            />
          </span>
          <el-tooltip
            v-else-if="row.status === 'failed'"
            :content="row.parseError || '解析失败'"
            placement="top"
          >
            <el-tag type="danger">✖ 失败(悬停看原因)</el-tag>
          </el-tooltip>
          <el-tag v-else type="info">◔ 排队中</el-tag>
          <el-tag v-if="!row.isActive" type="warning" size="small" class="gap-tag">待确认</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="责任人" width="100">
        <template #default="{ row }">{{ row.ownerName }}</template>
      </el-table-column>
      <el-table-column label="生效日期" width="110">
        <template #default="{ row }">{{ row.effectiveDate }}</template>
      </el-table-column>
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{ row }">
          <el-button v-if="row.status === 'failed'" link type="warning" @click="retry(row)">
            重试
          </el-button>
          <el-button v-if="!row.isActive" link type="success" @click="confirmRow(row)">
            确认激活
          </el-button>
          <el-tooltip content="暂不支持" placement="top">
            <span class="tip-host">
              <el-button link type="danger" disabled>删除</el-button>
            </span>
          </el-tooltip>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="还没有文档" :image-size="60">
          <el-button type="primary" size="small" @click="uploadVisible = true"
            >上传第一份文档</el-button
          >
        </el-empty>
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

    <UploadDialog :visible="uploadVisible" @close="uploadVisible = false" @uploaded="onUploaded" />
    <ZipImportDialog :visible="zipVisible" @close="zipVisible = false" @imported="onZipImported" />
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
/* disabled 按钮不产生鼠标事件：tooltip 挂在包裹 span 上（Element Plus 推荐姿势） */
.tip-host {
  display: inline-flex;
}
.ver {
  color: var(--el-text-color-secondary);
}
.parsing-cell {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.parsing-cell .bar {
  width: 90px;
}
.gap-tag {
  margin-left: 4px;
}
</style>
