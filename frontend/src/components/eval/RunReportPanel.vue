<script setup lang="ts">
/**
 * 跑分报告面板（F-603，api.md §4.2.7 + F-1012 阈值扫描区）：totals 四键卡 +
 * byDept/byQtype 两表 + 四指标卡与分型分组（metrics-balance 工单 07，spec §4.4
 * 设计师约定 ④⑤⑥）+ [阈值扫描]。报告 running/failed 也可出（部分明细+真实状态）。
 * 四指标 null 显 '—'（拒答类题目记 null 不计入，整卡 tooltip 注明）；byDocType
 * 单表同 byDept 款（短列头+全称 tooltip），docType 文案复用文档域 DOC_TYPE 映射。
 * 阈值扫描曲线 accuracy 为重放口径 ≠ 报告 accuracy（T5 评审裁决，文案注明）；
 * scope.note（qa 曲线口径局限）原样展示（T5 裁决）。
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
import { docTypeLabel } from '@/components/documents/options'
import { deptCodeLabel } from '@/components/admin/options'
import { formatPercent, qtypeLabel, runStatusTag } from './options'

/** 层级文案（评测集自动更新 07）：official=正式集 / extended=扩充集 */
function tierLabel(tier: string): string {
  return tier === 'official' ? '正式集' : '扩充集'
}

const props = defineProps<{ runId: number | null; visible: boolean }>()

const loading = ref(false)
const report = ref<EvalReport | null>(null)
const loadErr = ref('')
const scanning = ref(false)
const scanErr = ref('')
const scan = ref<ThresholdScanResult | null>(null)

/** 四指标卡（spec §4.4 ④：顺序固定 Recall/Precision/Relevancy/Correctness） */
const FOUR_METRIC_CARDS = [
  { key: 'contextRecall', label: 'Recall' },
  { key: 'contextPrecision', label: 'Precision' },
  { key: 'answerRelevancy', label: 'Relevancy' },
  { key: 'answerCorrectness', label: 'Correctness' },
] as const

/** byDocType 表四列（spec §4.4 ⑤：短列头 + 全称 tooltip） */
const DOC_TYPE_METRIC_COLUMNS = [
  {
    key: 'contextRecall',
    short: '召回',
    full: 'Context Recall：期望出处文档进入 context 的比例（机检）',
  },
  {
    key: 'contextPrecision',
    short: '精确',
    full: 'Context Precision：context 逐块含作答所需信息的排名加权（LLM judge）',
  },
  { key: 'answerRelevancy', short: '相关', full: 'Answer Relevancy：回答切题度（LLM judge）' },
  {
    key: 'answerCorrectness',
    short: '正确',
    full: 'Answer Correctness：回答正确性（judge 预填，人工评判优先合并）',
  },
] as const

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

/** 分型分组行文案：unassigned（未标注）是报告分组的合成组（来源未标注），非文档分型五档 */
function docTypeGroupLabel(docType: string): string {
  return docType === 'unassigned' ? '未标注' : docTypeLabel(docType)
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

      <h4 v-if="(report.byTier?.length ?? 0) > 0" class="sec-title">按题库层级</h4>
      <el-table
        v-if="(report.byTier?.length ?? 0) > 0"
        :data="report.byTier"
        border
        size="small"
        class="group-table"
      >
        <el-table-column label="层级" width="110">
          <template #default="{ row }">{{ tierLabel(row.tier) }}</template>
        </el-table-column>
        <el-table-column prop="total" label="题数" width="80" />
        <el-table-column label="准确率" width="110">
          <template #default="{ row }">{{ formatPercent(row.accuracy) }}</template>
        </el-table-column>
        <el-table-column label="说明" min-width="180">
          <template #default="{ row }">
            {{ row.tier === 'official' ? '发版门禁口径（正式集）' : '自动扩充集（不进门禁口径）' }}
          </template>
        </el-table-column>
      </el-table>
      <div
        v-if="report.vsPrevious"
        class="vs-prev"
        :class="{ degraded: report.vsPrevious.delta <= 0 }"
      >
        环比上一轮：{{ formatPercent(report.vsPrevious.previousAccuracy) }} →
        {{ formatPercent(report.totals.accuracy) }}
        （{{ report.vsPrevious.delta >= 0 ? '+' : ''
        }}{{ (report.vsPrevious.delta * 100).toFixed(1) }}pp）
      </div>

      <h4 class="sec-title" data-test="doc-type-title">按文档分型</h4>
      <div class="totals" data-test="doc-type-metrics">
        <el-tooltip
          v-for="card in FOUR_METRIC_CARDS"
          :key="card.key"
          content="拒答类题目记 null 不计入"
          placement="top"
        >
          <div class="total-card" :data-test="`metric-${card.key}`">
            <div class="num">
              {{
                report.fourMetrics[card.key].value === null
                  ? '—'
                  : formatPercent(report.fourMetrics[card.key].value as number)
              }}
            </div>
            <div class="lbl">{{ card.label }}</div>
            <div class="sub">参算 {{ report.fourMetrics[card.key].evaluatedCount }} 题</div>
          </div>
        </el-tooltip>
      </div>
      <el-empty
        v-if="report.byDocType.length === 0"
        description="本跑分暂无文档分型数据"
        :image-size="60"
        data-test="doc-type-empty"
      />
      <el-table
        v-else
        :data="report.byDocType"
        border
        size="small"
        class="group-table"
        data-test="doc-type-table"
      >
        <el-table-column label="分型" width="110">
          <template #default="{ row }">{{ docTypeGroupLabel(row.docType) }}</template>
        </el-table-column>
        <el-table-column prop="questionCount" label="题数" width="70" />
        <el-table-column v-for="col in DOC_TYPE_METRIC_COLUMNS" :key="col.key" width="92">
          <template #header>
            <el-tooltip :content="col.full" placement="top">
              <span>{{ col.short }}</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            {{ row[col.key] === null ? '—' : formatPercent(row[col.key] as number) }}
          </template>
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
.vs-prev {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.vs-prev.degraded {
  color: var(--el-color-danger);
}
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
.total-card .sub {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  margin-top: 4px;
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
