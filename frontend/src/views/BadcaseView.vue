<script setup lang="ts">
/**
 * P8 badcase 管理（ui-spec §七，对照原型 BadcaseView，F-702）：
 * 筛选行（部门/处理状态 + 查询/重置 §22.1）+ 表格（时间/部门/问题/分类/备注/状态/
 * 操作）+ 分页（10/20/50/100 默认 20 右下）+ 处置对话框（radio 处理方式 + 备注，
 * '已忽略' 必填理由前端先行校验 ADM-03，PATCH 422 兜底内联不关弹窗 §22.2）。
 * 默认视图显式传 badcaseStatus='待处理'（W3 交接：后端缺省不过滤）；重置后状态
 * 清空不过滤（对照原型）。仅'待处理'行出"处理"入口（终态无重开回退，api.md §4.3.4）。
 */
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { listBadcases, type BadcaseItem } from '@/api/badcase'
import {
  ADMIN_DEPT_ID_OPTIONS,
  BADCASE_STATUS_OPTIONS,
  badcaseStatusTag,
  deptCodeLabel,
} from '@/components/admin/options'
import LoadErrorState from '@/components/common/LoadErrorState.vue'
import HandleBadcaseDialog from '@/components/badcase/HandleBadcaseDialog.vue'

const filters = reactive({
  deptId: undefined as number | undefined,
  /** 默认视图 = 待处理（显式传参——后端缺省不过滤，W3 交接） */
  status: '待处理',
})
const items = ref<BadcaseItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
/** 加载失败标记（B11/工单 08）：失败 ≠ 没有数据，空态区呈现"可重试"而非"还没有" */
const loadError = ref(false)
const handleVisible = ref(false)
const handling = ref<BadcaseItem | null>(null)

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await listBadcases({
      deptId: filters.deptId,
      badcaseStatus: filters.status,
      page: page.value,
      pageSize: pageSize.value,
    })
    items.value = data.items
    total.value = data.total
    loadError.value = false
  } catch {
    // client 已统一 toast；此处标错误态供空态区呈现"失败可重试"（B11/工单 08）
    loadError.value = true
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

function openHandle(row: BadcaseItem): void {
  handling.value = row
  handleVisible.value = true
}

/** 处置成功：保持当前页刷新（§22.3 编辑类操作不回第 1 页） */
function onHandled(): void {
  ElMessage.success('已处理')
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
        placeholder="处理状态"
        clearable
        style="width: 140px"
        @change="search"
      >
        <el-option
          v-for="o in BADCASE_STATUS_OPTIONS"
          :key="o.value"
          :label="o.label"
          :value="o.value"
        />
      </el-select>
      <el-button type="primary" @click="search">查询</el-button>
      <el-button @click="resetFilters">重置</el-button>
    </div>

    <el-table v-loading="loading" :data="items" border stripe max-height="560">
      <el-table-column label="时间" width="150">
        <template #default="{ row }">{{ formatTime(row.createdAt) }}</template>
      </el-table-column>
      <el-table-column label="部门" width="90">
        <template #default="{ row }">{{ deptCodeLabel(row.deptCode) }}</template>
      </el-table-column>
      <el-table-column label="问题" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">{{ row.questionPreview }}</template>
      </el-table-column>
      <el-table-column label="分类" width="110">
        <template #default="{ row }">
          <el-tag v-if="row.category" size="small" type="danger">{{ row.category }}</el-tag>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column label="备注" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">{{ row.comment ?? '-' }}</template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="badcaseStatusTag(row.badcaseStatus).type" size="small">
            {{ badcaseStatusTag(row.badcaseStatus).text }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="100" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="(row.badcaseStatus ?? '待处理') === '待处理'"
            link
            type="primary"
            @click="openHandle(row)"
          >
            处理
          </el-button>
        </template>
      </el-table-column>
      <template #empty>
        <!-- 空态双分支（B11/工单 08）：加载失败可重试优先于真空态 -->
        <LoadErrorState v-if="loadError" data-test="badcases-load-error" @retry="load" />
        <el-empty v-else description="没有待处理的问题反馈" :image-size="60" />
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

    <HandleBadcaseDialog
      :visible="handleVisible"
      :row="handling"
      @close="handleVisible = false"
      @handled="onHandled"
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
</style>
