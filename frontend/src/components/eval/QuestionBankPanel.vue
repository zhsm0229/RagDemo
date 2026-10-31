<script setup lang="ts">
/**
 * 题库管理面板（评测集自动更新 02 票，api.md §4.2.9/§4.2.10）：
 * 评测题全量列表（origin/tier/dept/qtype/keyword 五维筛选）+ 层级一键升格/降格。
 * 原"题目列表无查询端点"的空态（deviations #41）随本面板消解。
 * 自动题（hardcase_synth/doc_synth）默认落 extended 层级；升格 official 才进
 * 发版门禁口径——操作列按钮按当前层级二态渲染。
 */
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import LoadErrorState from '@/components/common/LoadErrorState.vue'
import {
  listEvalQuestions,
  patchEvalQuestionTier,
  type EvalQuestionItem,
  type EvalQuestionOrigin,
  type EvalQuestionTier,
} from '@/api/evalAdmin'

const items = ref<EvalQuestionItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
/** 加载失败标记（B11/工单 08）：失败 ≠ 没有数据，空态区呈现"可重试"而非"还没有" */
const loadError = ref(false)
const filterOrigin = ref<string>('')
const filterTier = ref<string>('')
const filterDept = ref<string>('')
const filterKeyword = ref<string>('')

const ORIGIN_LABELS: Record<EvalQuestionOrigin, string> = {
  manual_import: '人工导入',
  hardcase_synth: '难例合成',
  doc_synth: '文档造题',
}
const TIER_LABELS: Record<EvalQuestionTier, string> = {
  official: '正式集',
  extended: '扩充集',
}
const DEPT_OPTIONS = [
  { value: 'service', label: '客服' },
  { value: 'hr', label: '人力' },
  { value: 'all', label: '全员' },
]
const QTYPE_LABELS: Record<string, string> = {
  factual: '事实型',
  multi_hop: '多跳',
  cross_reject: '串库拒答',
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await listEvalQuestions({
      page: page.value,
      pageSize: pageSize.value,
      origin: filterOrigin.value || undefined,
      tier: filterTier.value || undefined,
      dept: filterDept.value || undefined,
      keyword: filterKeyword.value || undefined,
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

/** 层级一键切换：extended→official=升格（进发版门禁），反之降格 */
async function onSwitchTier(row: EvalQuestionItem): Promise<void> {
  const target: EvalQuestionTier = row.tier === 'official' ? 'extended' : 'official'
  await patchEvalQuestionTier(row.id, target)
  ElMessage.success(target === 'official' ? '已升格为正式集' : '已降格为扩充集')
  void load()
}

function formatTime(iso: string): string {
  return iso.replace('T', ' ').slice(0, 16)
}
</script>

<template>
  <div>
    <div class="filter-row">
      <el-select v-model="filterOrigin" placeholder="来源" clearable style="width: 130px">
        <el-option
          v-for="(label, value) in ORIGIN_LABELS"
          :key="value"
          :value="value"
          :label="label"
        />
      </el-select>
      <el-select v-model="filterTier" placeholder="层级" clearable style="width: 110px">
        <el-option
          v-for="(label, value) in TIER_LABELS"
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
      <el-input
        v-model="filterKeyword"
        placeholder="问题关键词"
        clearable
        style="width: 200px"
        @keyup.enter="search"
      />
      <el-button type="primary" @click="search">查询</el-button>
    </div>
    <el-table v-loading="loading" :data="items" border stripe>
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="question" label="问题" min-width="220" show-overflow-tooltip />
      <el-table-column label="部门" width="80">
        <template #default="{ row }">
          {{ DEPT_OPTIONS.find((opt) => opt.value === row.deptCode)?.label ?? row.deptCode }}
        </template>
      </el-table-column>
      <el-table-column label="题型" width="100">
        <template #default="{ row }">{{ QTYPE_LABELS[row.qtype] ?? row.qtype }}</template>
      </el-table-column>
      <el-table-column label="来源" width="100">
        <template #default="{ row }">{{
          ORIGIN_LABELS[row.origin as EvalQuestionOrigin]
        }}</template>
      </el-table-column>
      <el-table-column label="层级" width="100">
        <template #default="{ row }">
          <el-tag :type="row.tier === 'official' ? 'success' : 'info'" size="small">
            {{ TIER_LABELS[row.tier as EvalQuestionTier] }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="导入时间" width="150">
        <template #default="{ row }">{{ formatTime(row.createdAt) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="100" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="onSwitchTier(row)">
            {{ row.tier === 'official' ? '降为扩充' : '升为正式' }}
          </el-button>
        </template>
      </el-table-column>
      <template #empty>
        <!-- 空态双分支（B11/工单 08）：加载失败可重试优先于真空态 -->
        <LoadErrorState v-if="loadError" data-test="eval-questions-load-error" @retry="load" />
        <el-empty
          v-else
          description="题库为空：下载模板并 Excel 导入第一批评测题"
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
</style>
