<script setup lang="ts">
/**
 * P7 提问记录（ui-spec §七，对照原型 MessagesView，F-701）：
 * 保留策略提示条 + 筛选行（部门/日期范围/是否点踩 + 查询/重置 §22.1）+ 表格
 * （时间/用户/部门/问题摘要+回答类型/引用/反馈/操作）+ 分页（10/20/50/100 默认
 * 20 右下）+ 详情抽屉（完整对话气泡只读置灰 §8.8、引用 JSON 折叠 readOnly、
 * token/耗时 meta；assistant=null 渲染中断占位）。
 * 列集按 ui-spec 文字规格（时间/用户/部门/问题摘要/是否有引用/反馈）+ DTO 键落列，
 * 原型"会话/回答摘要"列无对应字段（列表以 user 行为单位）→ deviations #38；
 * DTO 仅 hasCitations 布尔（无条数）、反馈无 category → 引用列"有/无"、反馈列仅
 * 点踩标记（同 #38）。日期范围经 checkDateRange 预检（§21.3）。
 * 空态双分支（B9，照 MemoryView §2.5 范式）：筛选无结果 vs 未积累。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { checkDateRange } from '@/validators'
import { listAdminMessages, type AdminMessageItem } from '@/api/adminMessages'
import { answerTypeLabel, ADMIN_DEPT_ID_OPTIONS, deptCodeLabel } from '@/components/admin/options'
import LoadErrorState from '@/components/common/LoadErrorState.vue'
import MessageDetailDrawer from '@/components/messages/MessageDetailDrawer.vue'

const filters = reactive({
  deptId: undefined as number | undefined,
  /** el-date-picker value-format YYYY-MM-DD，[from, to] 或 null */
  dateRange: null as [string, string] | null,
  fb: '' as '' | 'true' | 'false',
})
const filterErr = ref('')
const items = ref<AdminMessageItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
/** 加载失败标记（B11/工单 08）：失败 ≠ 没有数据，空态区呈现"可重试"而非"还没有" */
const loadError = ref(false)
const detailVisible = ref(false)
const detailId = ref<number | null>(null)

// 空态双分支判定（B9）：任一筛选条件激活即"筛选无结果"
const hasFilter = computed(
  () => filters.deptId !== undefined || filters.dateRange !== null || filters.fb !== '',
)

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await listAdminMessages({
      deptId: filters.deptId,
      dateFrom: filters.dateRange?.[0],
      dateTo: filters.dateRange?.[1],
      hasFeedback: filters.fb === '' ? undefined : filters.fb === 'true',
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
  filters.deptId = undefined
  filters.dateRange = null
  filters.fb = ''
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

function openDetail(row: AdminMessageItem): void {
  detailId.value = row.id
  detailVisible.value = true
}

/** DTO createdAt 为 ISO 字符串 → 原型口径 "YYYY-MM-DD HH:mm" */
function formatTime(iso: string): string {
  return iso.replace('T', ' ').slice(0, 16)
}
</script>

<template>
  <div>
    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="retain"
      title="提问记录保留 90 天（天数可配置），仅管理员可见；调优使用前先脱敏"
    />
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
      <el-date-picker
        v-model="filters.dateRange"
        type="daterange"
        value-format="YYYY-MM-DD"
        start-placeholder="开始日期"
        end-placeholder="结束日期"
        style="width: 240px"
      />
      <el-select
        v-model="filters.fb"
        placeholder="反馈"
        clearable
        style="width: 130px"
        @change="search"
      >
        <el-option label="有点踩" value="true" />
        <el-option label="无点踩" value="false" />
      </el-select>
      <el-button type="primary" @click="search">查询</el-button>
      <el-button @click="resetFilters">重置</el-button>
      <span v-if="filterErr" class="err-inline" role="alert">{{ filterErr }}</span>
    </div>

    <el-table v-loading="loading" :data="items" border stripe max-height="560">
      <el-table-column label="时间" width="150">
        <template #default="{ row }">{{ formatTime(row.createdAt) }}</template>
      </el-table-column>
      <el-table-column prop="username" label="用户" width="110" />
      <el-table-column label="部门" width="90">
        <template #default="{ row }">{{ deptCodeLabel(row.deptCode) }}</template>
      </el-table-column>
      <el-table-column label="问题摘要" min-width="240" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.questionPreview }}
          <el-tag size="small" type="info" class="type-tag">
            {{ answerTypeLabel(row.answerType) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="引用" width="80">
        <template #default="{ row }">
          <el-tag v-if="row.hasCitations" size="small" type="success">有</el-tag>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column label="反馈" width="100">
        <template #default="{ row }">
          <el-tag v-if="row.feedback === 'down'" type="danger" size="small">点踩</el-tag>
          <el-tag v-else-if="row.feedback === 'up'" type="success" size="small">点赞</el-tag>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openDetail(row)">详情</el-button>
        </template>
      </el-table-column>
      <!-- 空态三分支（B11/工单 08 + B9）：加载失败可重试 > 筛选无结果 > 未积累 -->
      <template #empty>
        <LoadErrorState v-if="loadError" data-test="messages-load-error" @retry="load" />
        <el-empty
          v-else-if="hasFilter"
          description="没有匹配筛选条件的提问记录"
          :image-size="60"
          data-test="messages-empty-filtered"
        >
          <el-button size="small" @click="resetFilters">清除筛选</el-button>
        </el-empty>
        <el-empty v-else description="暂无提问记录" :image-size="60" />
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

    <MessageDetailDrawer
      :visible="detailVisible"
      :message-id="detailId"
      @close="detailVisible = false"
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
.retain {
  margin-bottom: 12px;
}
.err-inline {
  color: var(--el-color-danger);
  font-size: 12px;
}
.type-tag {
  margin-left: 4px;
}
</style>
