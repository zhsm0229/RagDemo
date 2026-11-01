<script setup lang="ts">
/**
 * 检索测试结果卡（工单 30 自 RetrievalTestView 抽取，守 View 行数红线）：
 * qa 命中卡 + docs 两套分数表 + 阈值条——单次模式与对比模式共用同一渲染（同一
 * 契约 RetrievalTestResult）；props.title 显档位读数（对比模式）或缺省标题。
 * 分数口径红线（W3）：rerankScore 与 recallCosine 分列展示不互比。
 */
import type { RetrievalTestResult } from '@/api/retrievalTest'
import { fmtTime } from '@/utils/format'

defineProps<{
  title: string
  result: RetrievalTestResult
}>()

/** 历史命中格（F-1011）：次数 + 最近召回时刻；从未召回出占位文案不编时间 */
function fmtHistory(row: { hitCount: number; lastHitAt: string | null }): string {
  return `${row.hitCount} 次 · ${row.lastHitAt === null ? '从未被召回' : fmtTime(row.lastHitAt)}`
}
</script>

<template>
  <div class="result-card">
    <h4 class="card-title">{{ title }}</h4>
    <el-alert
      v-if="result.degraded"
      class="degraded-bar"
      type="warning"
      :closable="false"
      show-icon
      title="简化检索（rerank 熔断，rerankScore 为余弦口径，阈值对比结论不可信）"
    />
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

      <h4 class="section-title">文档块命中</h4>
      <el-table v-if="result.docs.length > 0" :data="result.docs" border size="small">
        <el-table-column label="#" width="50">
          <template #default="{ $index }">{{ $index + 1 }}</template>
        </el-table-column>
        <el-table-column label="重排分数（sigmoid）" width="150">
          <template #default="{ row }">{{ row.rerankScore }}</template>
        </el-table-column>
        <el-table-column label="召回余弦" width="100">
          <template #default="{ row }">{{ row.recallCosine }}</template>
        </el-table-column>
        <el-table-column label="文档" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.documentTitle }}</template>
        </el-table-column>
        <el-table-column label="页码" width="64">
          <template #default="{ row }">{{ row.page ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="标题路径" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">{{ row.titlePath ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="阈值" width="140">
          <template #default="{ row }">
            <el-tag v-if="row.thresholdPassed" type="success" size="small">✓ 超阈值</el-tag>
            <el-tag v-else type="danger" size="small">低于阈值</el-tag>
            <el-tag v-if="row.folded" type="info" size="small" class="gap-tag">已折叠</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="历史命中" width="170">
          <template #default="{ row }">{{ fmtHistory(row) }}</template>
        </el-table-column>
      </el-table>
      <div v-else class="muted">无文档块命中</div>

      <el-alert
        class="threshold-bar"
        type="info"
        :closable="false"
        show-icon
        :title="`当前生效阈值：问答对 ${result.thresholds.qa} · 拒答 ${result.thresholds.refuse} · topK ${result.params.topK}（只读，随响应下发）`"
      />
    </template>
  </div>
</template>
