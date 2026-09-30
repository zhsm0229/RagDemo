<script setup lang="ts">
/**
 * P5 检索测试（ui-spec §五，RAGFlow Retrieval Testing 模式，admin 只读调参工具 F-503）：
 * 部门范围下拉（service/hr/all，后端 deptCode 枚举仅此三者——原型第 4 项"全部（admin）"
 * 不渲染，deviations #28）+ 问题输入（strip 后 1-2000，checkQuestion 与 chat 同口径）
 * + 测试按钮 → 结果区：问答对命中卡（similarity=embedding 余弦 + thresholdPassed 标记）
 * + 文档块表（rerankScore=sigmoid 与 recallCosine=召回余弦分列展示，两套分数口径
 * 不互比——W3 交接红线；thresholdPassed/folded 逐块标记；历史命中列=W5 F-1011 读现列，
 * 对照"真实用户流量是否召回到过"）+ 当前阈值展示（响应下发只读，后端无阈值保存端点，
 * deviations #29）。422 错误内联（§22.2）；空结果（无 qa 且无 docs）展示空态引导（§8.5）。
 * 页内另设零召回块清单折叠区（F-1011 块级下钻，与健康度零召回榜同口径）：首展懒加载
 * + 分页，与左侧调参结果互不依赖。
 */
import { ref } from 'vue'
import { ApiClientError } from '@/api/client'
import { runRetrievalTest, type RetrievalTestResult } from '@/api/retrievalTest'
import ZeroRecallChunkPanel from '@/components/admin/ZeroRecallChunkPanel.vue'
import { checkQuestion } from '@/validators'

/** 部门范围选项：后端 deptCode 枚举 service/hr/all（api.md §4.4.1），默认客服（对照原型） */
const SCOPE_OPTIONS = [
  { value: 'service', label: '客服' },
  { value: 'hr', label: '人力' },
  { value: 'all', label: '全员' },
] as const

const scope = ref<'service' | 'hr' | 'all'>('service')
const question = ref('')
const questionErr = ref('')
const error = ref('')
const testing = ref(false)
const result = ref<RetrievalTestResult | null>(null)

async function run(): Promise<void> {
  questionErr.value = ''
  error.value = ''
  const check = checkQuestion(question.value)
  if (check !== true) {
    questionErr.value = check
    return
  }
  testing.value = true
  try {
    result.value = await runRetrievalTest({
      question: question.value.trim(),
      deptCode: scope.value,
    })
  } catch (e) {
    // 422（question 长度 / deptCode 枚举）内联展示，上一轮结果保留不闪空（§22.2）
    error.value = e instanceof ApiClientError ? e.message : '系统繁忙，请稍后重试'
  } finally {
    testing.value = false
  }
}

/** ISO8601 → YYYY-MM-DD HH:mm（同 OverviewView/AuditView 口径） */
function fmtTime(iso: string): string {
  return iso.replace('T', ' ').slice(0, 16)
}

/** 历史命中格（F-1011）：次数 + 最近召回时刻；从未召回出占位文案不编时间 */
function fmtHistory(row: { hitCount: number; lastHitAt: string | null }): string {
  return `${row.hitCount} 次 · ${row.lastHitAt === null ? '从未被召回' : fmtTime(row.lastHitAt)}`
}
</script>

<template>
  <div class="page">
    <div class="panel">
      <h4>检索测试（admin 调试工具 · F-503）</h4>
      <div class="filter-row">
        <span class="field-label">部门范围</span>
        <el-select v-model="scope" style="width: 140px">
          <el-option v-for="o in SCOPE_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
        </el-select>
      </div>
      <div class="filter-row">
        <el-input
          v-model="question"
          placeholder="输入测试问题，例如：退货运费谁承担 / 年假怎么算"
          maxlength="2000"
          style="max-width: 520px"
          @keyup.enter="run"
        />
        <el-button type="primary" :loading="testing" @click="run">测 试</el-button>
      </div>
      <div v-if="questionErr" class="err-inline" role="alert">{{ questionErr }}</div>
      <div v-if="error" class="err-inline" role="alert">{{ error }}</div>

      <template v-if="result">
        <el-empty
          v-if="result.qa === null && result.docs.length === 0"
          description="未命中任何问答对或文档块"
          :image-size="80"
        >
          <span class="empty-hint">可换个问法重试，或调整部门范围；确认语料已导入并解析就绪</span>
        </el-empty>
        <template v-else>
          <h4 class="section-title">问答对命中</h4>
          <template v-if="result.qa">
            <el-tag type="success">
              FAQ-{{ result.qa.id }} 相似度 {{ result.qa.similarity }} ✓ 超阈值({{
                result.thresholds.qa
              }})，将整条注入
            </el-tag>
            <div class="qa-question">{{ result.qa.question }}</div>
          </template>
          <el-tag v-else type="info">无超阈值命中 → 走文档检索链路</el-tag>

          <h4 class="section-title">文档块命中（top20 → 重排 top5）</h4>
          <el-table v-if="result.docs.length > 0" :data="result.docs" border size="small">
            <el-table-column label="#" width="50">
              <template #default="{ $index }">{{ $index + 1 }}</template>
            </el-table-column>
            <el-table-column label="重排分数（sigmoid）" width="160">
              <template #default="{ row }">{{ row.rerankScore }}</template>
            </el-table-column>
            <el-table-column label="召回余弦" width="110">
              <template #default="{ row }">{{ row.recallCosine }}</template>
            </el-table-column>
            <el-table-column label="文档" min-width="170" show-overflow-tooltip>
              <template #default="{ row }">{{ row.documentTitle }}</template>
            </el-table-column>
            <el-table-column label="页码" width="70">
              <template #default="{ row }">{{ row.page ?? '—' }}</template>
            </el-table-column>
            <el-table-column label="标题路径" min-width="160" show-overflow-tooltip>
              <template #default="{ row }">{{ row.titlePath ?? '—' }}</template>
            </el-table-column>
            <el-table-column label="阈值" width="150">
              <template #default="{ row }">
                <el-tag v-if="row.thresholdPassed" type="success" size="small">✓ 超阈值</el-tag>
                <el-tag v-else type="danger" size="small">低于阈值</el-tag>
                <el-tag v-if="row.folded" type="info" size="small" class="gap-tag">已折叠</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="历史命中" width="180">
              <template #default="{ row }">{{ fmtHistory(row) }}</template>
            </el-table-column>
          </el-table>
          <div v-else class="muted">无文档块命中</div>

          <el-alert
            class="threshold-bar"
            type="info"
            :closable="false"
            show-icon
            :title="`当前生效阈值：问答对 ${result.thresholds.qa} · 拒答 ${result.thresholds.refuse}（只读，随检索测试响应下发）`"
          />
        </template>
      </template>
    </div>

    <div class="panel">
      <!-- 零召回块清单（F-1011 块级下钻）：独立面板组件，与调参结果互不依赖 -->
      <ZeroRecallChunkPanel />
    </div>
  </div>
</template>

<style scoped>
/* 对照原型 .panel 宽度（max-width:900px）与 .filter-row 同行排布 */
.page {
  max-width: 900px;
}
.page > .panel + .panel {
  margin-top: 12px;
}
.filter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.field-label {
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.section-title {
  margin: 16px 0 10px;
}
.qa-question {
  font-size: 13px;
  color: var(--el-text-color-regular);
  margin-top: 6px;
}
.muted {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.gap-tag {
  margin-left: 4px;
}
.threshold-bar {
  margin-top: 10px;
}
.empty-hint {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.err-inline {
  color: var(--el-color-danger);
  font-size: 12px;
  margin-top: 4px;
}
</style>
