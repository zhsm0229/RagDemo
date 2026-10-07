<script setup lang="ts">
/**
 * 跑分报告面板（F-603，api.md §4.2.7 + F-1012 阈值扫描区）：totals 四键卡 +
 * byDept/byQtype 两表 + [阈值扫描]。报告 running/failed 也可出（部分明细+真实状态）。
 * 阈值扫描端点已随 feat/w4-fe 合并（5878a01）：T13 评审 #44 解禁，扫描入口
 * 无条件启用（历史开关 FEATURE_FLAGS 已移除）。曲线 accuracy 为重放口径
 * ≠ 报告 accuracy（T5 评审裁决，文案注明）；scope.note（qa 曲线口径局限）原样
 * 展示（T5 裁决）。
 */
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ApiClientError } from '@/api/client'
import {
  getEvalReport,
  runThresholdScan,
  type EvalReport,
  type ThresholdScanResult,
} from '@/api/evalAdmin'
import { deptCodeLabel } from '@/components/admin/options'
import { formatPercent, qtypeLabel, runStatusTag } from './options'

const props = defineProps<{ runId: number | null; visible: boolean }>()

const loading = ref(false)
const report = ref<EvalReport | null>(null)
const loadErr = ref('')
const scanning = ref(false)
const scanErr = ref('')
const scan = ref<ThresholdScanResult | null>(null)

// immediate：面板随抽屉内容渲染时 visible 已为 true（v-else-if 之后再挂载），
// 不加 immediate 首次打开不触发报告加载
watch(
  () => props.visible,
  (visible) => {
    if (visible) void load()
  },
  { immediate: true },
)

async function load(): Promise<void> {
  if (props.runId === null) return
  loading.value = true
  report.value = null
  scan.value = null
  loadErr.value = ''
  try {
    report.value = await getEvalReport(props.runId)
  } catch {
    loadErr.value = '报告加载失败，请稍后重试'
  } finally {
    loading.value = false
  }
}

async function doScan(): Promise<void> {
  if (scanning.value || props.runId === null) return
  scanning.value = true
  scanErr.value = ''
  try {
    scan.value = await runThresholdScan(props.runId)
  } catch (e) {
    scanErr.value = e instanceof ApiClientError ? e.message : '系统繁忙，请稍后重试'
  } finally {
    scanning.value = false
  }
}

/** 推荐档高亮判定：曲线档位等于推荐值（推荐值为 null 不高亮） */
function isRecommended(threshold: number, recommended: number | null): boolean {
  return recommended !== null && threshold === recommended
}

function copyScan(): void {
  void navigator.clipboard?.writeText(JSON.stringify(scan.value, null, 2))
  ElMessage.success('扫描结果已复制为 JSON')
}

defineExpose({ load, doScan })
</script>

<template>
  <section v-loading="loading" class="report">
    <h4 class="sec-title">分组报告</h4>
    <el-alert v-if="loadErr" type="error" :title="loadErr" :closable="false" />
    <template v-else-if="report">
      <p v-if="report.status !== 'done'" class="partial-note" data-test="report-partial-note">
        当前状态：{{ runStatusTag(report.status).text }}——以下为已完成明细的部分报告，非最终口径
      </p>
      <div class="totals">
        <div class="total-card">
          <div class="num">{{ report.totals.total }}</div>
          <div class="lbl">总题数</div>
        </div>
        <div class="total-card">
          <div class="num">{{ report.totals.correct }}</div>
          <div class="lbl">加权得分</div>
        </div>
        <div class="total-card">
          <div class="num">{{ formatPercent(report.totals.accuracy) }}</div>
          <div class="lbl">总体准确率</div>
        </div>
        <div class="total-card">
          <div class="num">{{ report.totals.pendingCount }}</div>
          <div class="lbl">待判定</div>
        </div>
      </div>
      <el-table :data="report.byDept" border size="small" class="group-table">
        <el-table-column label="部门" width="110">
          <template #default="{ row }">{{ deptCodeLabel(row.deptCode) }}</template>
        </el-table-column>
        <el-table-column prop="total" label="题数" width="80" />
        <el-table-column label="准确率" width="110">
          <template #default="{ row }">{{ formatPercent(row.accuracy) }}</template>
        </el-table-column>
      </el-table>
      <el-table :data="report.byQtype" border size="small" class="group-table">
        <el-table-column label="题型" width="110">
          <template #default="{ row }">{{ qtypeLabel(row.qtype) }}</template>
        </el-table-column>
        <el-table-column prop="total" label="题数" width="80" />
        <el-table-column label="准确率" width="110">
          <template #default="{ row }">{{ formatPercent(row.accuracy) }}</template>
        </el-table-column>
      </el-table>
    </template>

    <h4 class="sec-title">阈值扫描</h4>
    <div class="scan-bar">
      <el-button type="primary" :loading="scanning" data-test="scan-btn" @click="doScan"
        >扫描阈值曲线</el-button
      >
      <span v-if="scan" class="muted">
        推荐值：拒答阈值 {{ scan.recommended.refuseThreshold ?? '-' }} / 问答对阈值
        {{ scan.recommended.qaThreshold ?? '-' }} · 已排除"全员拒答"档
      </span>
    </div>
    <div v-if="scanErr" class="err-inline" role="alert">{{ scanErr }}</div>
    <template v-if="scan">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        class="scope-note"
        title="口径注明：曲线 accuracy 为阈值重放口径，≠ 报告 accuracy（明细聚合）；曲线仅用于横向比较各档位"
      />
      <el-alert
        type="info"
        :closable="false"
        show-icon
        class="scope-note"
        :title="scan.scope.note"
      />
      <el-table
        :data="scan.refuseCurve"
        border
        size="small"
        class="group-table"
        data-test="refuse-curve"
      >
        <el-table-column label="拒答阈值" width="100">
          <template #default="{ row }">
            <span
              :class="{
                recommended: isRecommended(row.threshold, scan!.recommended.refuseThreshold),
              }"
            >
              {{ row.threshold }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="重放准确率" width="110">
          <template #default="{ row }">{{ formatPercent(row.accuracy) }}</template>
        </el-table-column>
        <el-table-column label="拒答率" width="110">
          <template #default="{ row }">{{ formatPercent(row.refuseRate) }}</template>
        </el-table-column>
      </el-table>
      <el-table :data="scan.qaCurve" border size="small" class="group-table" data-test="qa-curve">
        <el-table-column label="问答对阈值" width="100">
          <template #default="{ row }">
            <span
              :class="{ recommended: isRecommended(row.threshold, scan!.recommended.qaThreshold) }"
            >
              {{ row.threshold }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="命中率" width="110">
          <template #default="{ row }">{{ formatPercent(row.hitRate) }}</template>
        </el-table-column>
      </el-table>
      <el-button link type="primary" size="small" @click="copyScan">复制扫描结果 JSON</el-button>
    </template>
  </section>
</template>

<style scoped>
.sec-title {
  margin: 18px 0 10px;
  font-size: 14px;
}
.partial-note {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--el-color-warning);
}
.totals {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}
.total-card {
  flex: 1;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  padding: 10px;
  text-align: center;
}
.total-card .num {
  font-size: 22px;
  font-weight: 600;
}
.total-card .lbl {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
.group-table {
  margin-bottom: 12px;
}
.scan-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}
.scope-note {
  margin-bottom: 8px;
}
.recommended {
  color: var(--el-color-success);
  font-weight: 600;
}
.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.err-inline {
  color: var(--el-color-danger);
  font-size: 12px;
  margin-bottom: 8px;
}
</style>
