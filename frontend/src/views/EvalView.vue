<script setup lang="ts">
/**
 * P9 评测管理（ui-spec §八，对照原型 EvalView，F-601/602/603）：
 * 题库 tab（QuestionBankPanel：全量列表 + 五维筛选 + 层级升格/降格——原"无查询
 * 端点空态"（deviations #41）随 §4.2.9 列表端点上线消解；Excel 导入 + 模板下载
 * 保留本 tab 工具行）+ 跑分记录 tab（[发起跑分] 名称+视角部门；列表 名称/状态/
 * 总数/正确/准确率/时间——原型"模型/提示词版本"列不在列表 DTO，config 快照在
 * 详情展示，deviations #43；running 行每 10s 轮询列表刷新状态）+ 详情抽屉
 * （逐题明细/录分/报告/阈值扫描，见 RunDetailDrawer）。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import {
  listEvalRuns,
  EVAL_QUESTIONS_TEMPLATE_FILENAME,
  EVAL_QUESTIONS_TEMPLATE_URL,
  type EvalRunItem,
} from '@/api/evalAdmin'
import { formatPercent, runStatusTag } from '@/components/eval/options'
import LoadErrorState from '@/components/common/LoadErrorState.vue'
import CreateRunDialog from '@/components/eval/CreateRunDialog.vue'
import EvalAutoSettingsPanel from '@/components/eval/EvalAutoSettingsPanel.vue'
import HardcasePoolPanel from '@/components/eval/HardcasePoolPanel.vue'
import ImportEvalDialog from '@/components/eval/ImportEvalDialog.vue'
import QuestionBankPanel from '@/components/eval/QuestionBankPanel.vue'
import RunDetailDrawer from '@/components/eval/RunDetailDrawer.vue'

const LIST_POLL_INTERVAL_MS = 10_000

const tab = ref('runs')
const items = ref<EvalRunItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
/** 加载失败标记（B11/工单 08）：失败 ≠ 没有数据，空态区呈现"可重试"而非"还没有" */
const loadError = ref(false)
const importVisible = ref(false)
const createVisible = ref(false)
const drawerVisible = ref(false)
const drawerRunId = ref<number | null>(null)
let listPollTimer: number | null = null

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await listEvalRuns({ page: page.value, pageSize: pageSize.value })
    items.value = data.items
    total.value = data.total
    loadError.value = false
    syncPolling(data.items)
  } catch {
    // client 已统一 toast；此处标错误态供空态区呈现"失败可重试"（B11/工单 08）
    loadError.value = true
  } finally {
    loading.value = false
  }
}

onMounted(() => void load())
onBeforeUnmount(stopPolling)

/** 任一行 running 即每 10s 刷新列表（状态与准确率收口后自动停止） */
function syncPolling(rows: EvalRunItem[]): void {
  stopPolling()
  if (rows.some((row) => row.status === 'running')) {
    listPollTimer = window.setInterval(() => void load(), LIST_POLL_INTERVAL_MS)
  }
}

function stopPolling(): void {
  if (listPollTimer !== null) {
    clearInterval(listPollTimer)
    listPollTimer = null
  }
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

/** 模板下载：同源 <a download> 直链，Cookie 随请求携带（不走 fetch 封装） */
function downloadTemplate(): void {
  const a = document.createElement('a')
  a.href = EVAL_QUESTIONS_TEMPLATE_URL
  a.download = EVAL_QUESTIONS_TEMPLATE_FILENAME
  a.click()
}

function openReport(row: EvalRunItem): void {
  drawerRunId.value = row.id
  drawerVisible.value = true
}

/** 发起跑分（202）：关弹窗回第 1 页刷新，running 行立即可见并触发轮询 */
function onCreated(): void {
  createVisible.value = false
  page.value = 1
  void load()
}

/** 抽屉内录分/状态收口后同步列表 */
function onRunChanged(): void {
  void load()
}

/** DTO createdAt 为 ISO 字符串 → 原型口径 "YYYY-MM-DD HH:mm" */
function formatTime(iso: string): string {
  return iso.replace('T', ' ').slice(0, 16)
}
</script>

<template>
  <div>
    <el-tabs v-model="tab">
      <el-tab-pane label="评测题库" name="questions">
        <div class="filter-row">
          <span class="spacer"></span>
          <el-button @click="downloadTemplate">下载模板</el-button>
          <el-button type="primary" @click="importVisible = true">Excel 导入题目</el-button>
        </div>
        <QuestionBankPanel />
      </el-tab-pane>

      <el-tab-pane label="难例池" name="hardcase">
        <HardcasePoolPanel />
      </el-tab-pane>

      <el-tab-pane label="自动评测设置" name="auto" lazy>
        <EvalAutoSettingsPanel />
      </el-tab-pane>

      <el-tab-pane label="跑分记录" name="runs">
        <div class="filter-row">
          <el-tooltip content="评测按题库逐题走问答链路，串库题验证固定话术拒答" placement="top">
            <el-button type="primary" @click="createVisible = true">发起跑分</el-button>
          </el-tooltip>
        </div>
        <el-table v-loading="loading" :data="items" border stripe>
          <el-table-column prop="name" label="名称" min-width="160" />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="runStatusTag(row.status).type" size="small">
                {{ runStatusTag(row.status).text }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="总数/正确" width="110">
            <template #default="{ row }">{{ row.total }}/{{ row.correct }}</template>
          </el-table-column>
          <el-table-column label="总体准确率" width="110">
            <template #default="{ row }">
              <el-tag :type="row.accuracy >= 0.8 ? 'success' : 'danger'" size="small">
                {{ formatPercent(row.accuracy) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="时间" width="150">
            <template #default="{ row }">{{ formatTime(row.createdAt) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openReport(row)">报告</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <!-- 空态双分支（B11/工单 08）：加载失败可重试优先于真空态 -->
            <LoadErrorState v-if="loadError" data-test="eval-runs-load-error" @retry="load" />
            <el-empty v-else description="还没有跑分任务" :image-size="60">
              <el-button type="primary" size="small" @click="createVisible = true">
                发起第一次跑分
              </el-button>
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
      </el-tab-pane>
    </el-tabs>

    <ImportEvalDialog :visible="importVisible" @close="importVisible = false" />
    <CreateRunDialog :visible="createVisible" @close="createVisible = false" @created="onCreated" />
    <RunDetailDrawer
      :visible="drawerVisible"
      :run-id="drawerRunId"
      @close="drawerVisible = false"
      @changed="onRunChanged"
    />
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
