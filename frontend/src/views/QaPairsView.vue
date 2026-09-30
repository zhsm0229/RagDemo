<script setup lang="ts">
/**
 * P4 知识库·问答对管理（ui-spec §四，对照原型 QaView）：
 * 筛选行（部门/状态/关键字 + 查询/重置同行 §22.1，下拉变更即刷新）+ 工具栏
 * （下载导入模板 / Excel 批量导入——原型无"新建问答对"按钮，后端亦无单条新建端点，
 * 建语料仅批量导入入口）+ 表格（问题/答案/部门/创建时间/启停 switch/批次/操作）+
 * 分页（10/20/50/100 默认 20 右下）。
 * 启停 switch 即时生效（F-402，开关即刷无确认弹窗），失败回滚开关（client 统一
 * toast 错误）；编辑/删除后保持当前页刷新（§22.3，删空回退一页）；删除 el-popconfirm
 * 二次确认（对照原型）；列表 DTO 无 updatedAt（api.md §4.1.3），时间列以 createdAt
 * 呈现（deviations #30）。部门列按真实 deptCode 渲染中文（后端 join departments
 * 下发库内编码，api.md §4.1.3 9 键），不再按种子序 id 映射（T11 裁定脆弱点已除）。
 */
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  QA_PAIRS_TEMPLATE_FILENAME,
  QA_PAIRS_TEMPLATE_URL,
  deleteQaPair,
  listQaPairs,
  toggleQaPairStatus,
  type QaPairItem,
  type QaPairStatus,
} from '@/api/qaPairs'
import { ADMIN_DEPT_ID_OPTIONS, deptCodeLabel } from '@/components/admin/options'
import { QA_STATUS_OPTIONS } from '@/components/qa/options'
import EditQaDialog from '@/components/qa/EditQaDialog.vue'
import ImportQaDialog from '@/components/qa/ImportQaDialog.vue'

const filters = reactive({
  deptId: undefined as number | undefined,
  status: '' as QaPairStatus | '',
  keyword: '',
})
const items = ref<QaPairItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const importVisible = ref(false)
const editVisible = ref(false)
const editing = ref<QaPairItem | null>(null)

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await listQaPairs({
      deptId: filters.deptId,
      status: filters.status,
      keyword: filters.keyword,
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
  page.value = 1
  void load()
}

function resetFilters(): void {
  filters.deptId = undefined
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

/** 模板下载（F-403）：同源 <a download> 直链，Cookie 随请求携带（不走 fetch 封装） */
function downloadTemplate(): void {
  const a = document.createElement('a')
  a.href = QA_PAIRS_TEMPLATE_URL
  a.download = QA_PAIRS_TEMPLATE_FILENAME
  document.body.appendChild(a)
  a.click()
  a.remove()
}

/** 启停即时生效（F-402）：v-model 先行更新，失败回滚；成功后保持当前页刷新 */
async function onToggle(row: QaPairItem): Promise<void> {
  const prev: QaPairStatus = row.status === 'enabled' ? 'disabled' : 'enabled'
  try {
    await toggleQaPairStatus(row.id, row.status)
  } catch {
    row.status = prev
    return
  }
  ElMessage.success(row.status === 'enabled' ? '已启用，立即参与匹配' : '已停用，立即退出匹配')
  void load()
}

function openEdit(row: QaPairItem): void {
  editing.value = row
  editVisible.value = true
}

function onEdited(): void {
  void load()
}

async function removeRow(row: QaPairItem): Promise<void> {
  await deleteQaPair(row.id)
  ElMessage.success('已删除')
  // §22.3：删除后保持当前页刷新，当前页被删空时回退一页
  if (items.value.length <= 1 && page.value > 1) page.value -= 1
  void load()
}

/** 导入成功：回第 1 页刷新让新批次立即可见（§22.3；弹窗内成功态由用户关闭） */
function onImported(): void {
  page.value = 1
  void load()
}

/** DTO createdAt 为 ISO 字符串 → 原型口径 "YYYY-MM-DD HH:mm" */
function formatTime(iso: string): string {
  return iso.replace('T', ' ').slice(0, 16)
}
</script>

<template>
  <div>
    <div class="filter-row">
      <el-select
        v-model="filters.deptId"
        placeholder="部门"
        clearable
        style="width: 130px"
        @change="search"
      >
        <el-option
          v-for="o in ADMIN_DEPT_ID_OPTIONS"
          :key="o.value"
          :label="o.label"
          :value="o.value"
        />
      </el-select>
      <el-select
        v-model="filters.status"
        placeholder="状态"
        clearable
        style="width: 130px"
        @change="search"
      >
        <el-option
          v-for="o in QA_STATUS_OPTIONS"
          :key="o.value"
          :label="o.label"
          :value="o.value"
        />
      </el-select>
      <el-input
        v-model="filters.keyword"
        placeholder="问题关键字"
        style="width: 200px"
        clearable
        @keyup.enter="search"
        @clear="search"
      />
      <el-button type="primary" @click="search">查询</el-button>
      <el-button @click="resetFilters">重置</el-button>
      <span class="spacer"></span>
      <el-button @click="downloadTemplate">下载导入模板</el-button>
      <el-button type="primary" @click="importVisible = true">Excel 批量导入</el-button>
    </div>

    <el-table v-loading="loading" :data="items" border stripe>
      <el-table-column label="问题" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">{{ row.question }}</template>
      </el-table-column>
      <el-table-column label="答案" min-width="300" show-overflow-tooltip>
        <template #default="{ row }">{{ row.answer }}</template>
      </el-table-column>
      <el-table-column label="部门" width="90">
        <template #default="{ row }">{{ deptCodeLabel(row.deptCode) }}</template>
      </el-table-column>
      <el-table-column label="创建时间" width="150">
        <template #default="{ row }">{{ formatTime(row.createdAt) }}</template>
      </el-table-column>
      <el-table-column label="启用" width="90">
        <template #default="{ row }">
          <el-switch
            v-model="row.status"
            active-value="enabled"
            inactive-value="disabled"
            @change="onToggle(row)"
          />
        </template>
      </el-table-column>
      <el-table-column label="批次" width="150">
        <template #default="{ row }">{{ row.batchNo }}</template>
      </el-table-column>
      <el-table-column label="操作" width="130" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-popconfirm title="确认删除该问答对？" @confirm="removeRow(row)">
            <template #reference>
              <el-button link type="danger">删除</el-button>
            </template>
          </el-popconfirm>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="还没有问答对" :image-size="60">
          <el-button type="primary" size="small" @click="importVisible = true"
            >Excel 批量导入</el-button
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

    <ImportQaDialog
      :visible="importVisible"
      @close="importVisible = false"
      @imported="onImported"
    />
    <EditQaDialog
      :visible="editVisible"
      :qa="editing"
      @close="editVisible = false"
      @saved="onEdited"
    />
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
</style>
