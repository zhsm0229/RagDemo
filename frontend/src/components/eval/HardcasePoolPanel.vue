<script setup lang="ts">
/**
 * 难例池面板（评测集自动更新 03 票，api.md §4.2.11/§4.2.12/§4.2.13）：
 * 真实拒答/点踩问题种子列表（status/dept 筛选）+ 手动触发合成 + 置忽略终态。
 * 种子是"评测素材"，不是 badcase 治理队列（处置流仍在提问记录·badcase 页）——
 * 本面板只做素材管理：忽略=不再参与合成。
 */
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import LoadErrorState from '@/components/common/LoadErrorState.vue'
import {
  ignoreHardcaseSeed,
  listHardcaseSeeds,
  synthesizeHardcaseSeeds,
  type HardcaseSeedItem,
  type HardcaseSeedStatus,
} from '@/api/evalAdmin'

const items = ref<HardcaseSeedItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const synthesizing = ref(false)
/** 加载失败标记（B11/工单 08）：失败 ≠ 没有数据，空态区呈现"可重试"而非"还没有" */
const loadError = ref(false)
const filterStatus = ref<string>('')
const filterDept = ref<string>('')

const STATUS_LABELS: Record<HardcaseSeedStatus, string> = {
  pending: '待合成',
  synthesized: '已合成',
  no_ground: '待补语料',
  ignored: '已忽略',
}
const STATUS_TAGS: Record<HardcaseSeedStatus, string> = {
  pending: 'primary',
  synthesized: 'success',
  no_ground: 'warning',
  ignored: 'info',
}
const SOURCE_LABELS: Record<string, string> = {
  refuse: '拒答',
  feedback_down: '点踩',
}
const DEPT_OPTIONS = [
  { value: 'service', label: '客服' },
  { value: 'hr', label: '人力' },
  { value: 'all', label: '全员' },
]

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await listHardcaseSeeds({
      page: page.value,
      pageSize: pageSize.value,
      status: filterStatus.value || undefined,
      dept: filterDept.value || undefined,
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

function search(): void {
  page.value = 1
  void load()
}

onMounted(() => void load())

/** 手动合成：同步接口（预算内 pending 种子批量出题），完成后刷新并报摘要 */
async function onSynthesize(): Promise<void> {
  synthesizing.value = true
  try {
    const summary = await synthesizeHardcaseSeeds()
    ElMessage.success(
      `合成完成：新增 ${summary.synthesized} 题；待补语料 ${summary.noGround}，跳过 ${summary.skipped}`,
    )
    void load()
  } finally {
    synthesizing.value = false
  }
}

/** 忽略=不可恢复终态（工单 08，评审要求二次确认）：取消静默返回不发请求 */
async function onIgnore(row: HardcaseSeedItem): Promise<void> {
  try {
    await ElMessageBox.confirm('忽略后该种子不再参与自动合成，不可恢复。确认忽略？', '忽略种子', {
      type: 'warning',
      confirmButtonText: '确认忽略',
      cancelButtonText: '取消',
    })
  } catch {
    return // 用户取消：归取消，不发请求
  }
  await ignoreHardcaseSeed(row.id)
  ElMessage.success('已忽略：该种子不再参与合成')
  void load()
}

function formatTime(iso: string): string {
  return iso.replace('T', ' ').slice(0, 16)
}
</script>

<template>
  <div>
    <div class="filter-row">
      <el-select v-model="filterStatus" placeholder="状态" clearable style="width: 120px">
        <el-option
          v-for="(label, value) in STATUS_LABELS"
          :key="value"
          :value="value"
          :label="label"
        />
      </el-select>
      <el-select v-model="filterDept" placeholder="部门" clearable style="width: 110px">
        <el-option
          v-for="opt in DEPT_OPTIONS"
          :key="opt.value"
          :value="opt.value"
          :label="opt.label"
        />
      </el-select>
      <el-button type="primary" @click="search">查询</el-button>
      <span class="spacer"></span>
      <el-tooltip
        content="对全部「待合成」种子批量生成变体评测题（入扩充集，不进发版门禁口径）"
        placement="top"
      >
        <el-button type="primary" :loading="synthesizing" @click="onSynthesize"
          >生成变体题</el-button
        >
      </el-tooltip>
    </div>
    <el-table v-loading="loading" :data="items" border stripe>
      <el-table-column prop="questionText" label="用户问题" min-width="220" show-overflow-tooltip />
      <el-table-column label="来源" width="80">
        <template #default="{ row }">{{
          SOURCE_LABELS[row.sourceType] ?? row.sourceType
        }}</template>
      </el-table-column>
      <el-table-column label="部门" width="80">
        <template #default="{ row }">
          {{ DEPT_OPTIONS.find((opt) => opt.value === row.deptCode)?.label ?? row.deptCode }}
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="STATUS_TAGS[row.status as HardcaseSeedStatus]" size="small">
            {{ STATUS_LABELS[row.status as HardcaseSeedStatus] }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="synthCount" label="已产题数" width="90" />
      <el-table-column label="入池时间" width="150">
        <template #default="{ row }">{{ formatTime(row.createdAt) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button v-if="row.status !== 'ignored'" link type="danger" @click="onIgnore(row)">
            忽略
          </el-button>
        </template>
      </el-table-column>
      <template #empty>
        <!-- 空态双分支（B11/工单 08）：加载失败可重试优先于真空态 -->
        <LoadErrorState v-if="loadError" data-test="hardcase-seeds-load-error" @retry="load" />
        <el-empty
          v-else
          description="难例池为空：用户拒答与点踩问题会按周自动沉淀入池"
          :image-size="60"
        />
      </template>
    </el-table>
    <div class="pager-row">
      <el-pagination
        :current-page="page"
        :page-size="pageSize"
        :page-sizes="[10, 20, 50, 100]"
        :total="total"
        layout="total, sizes, prev, pager, next"
        @current-change="(next: number) => ((page = next), void load())"
        @size-change="(size: number) => ((pageSize = size), (page = 1), void load())"
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
