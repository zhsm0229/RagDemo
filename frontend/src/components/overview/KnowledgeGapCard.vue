<script setup lang="ts">
/**
 * 知识缺口 TOP 卡片（F-1010，P10 概览页）：拒答问题按周聚合榜——top10 表格
 * （问题/出现次数）+ 周切换下拉（本周/上周/前两周）+ 导出 CSV（fetch blob →
 * a[download] 触发，同 AuditView 口径；X-Truncated 提示"已截断"）；空态引导
 * "暂无拒答——运行评测或等待用户提问"。数据自取自挂载（api.md §4.9）：端点按
 * count 降序取前 50，卡片只展示 top10，超出提示导出可看全部。
 */
import { computed, onMounted, ref } from 'vue'
import { ApiClientError } from '@/api/client'
import {
  exportKnowledgeGap,
  getKnowledgeGap,
  type KnowledgeGapData,
  type WeekOffset,
} from '@/api/knowledgeGap'

const GAP_WEEK_LABELS: Record<WeekOffset, string> = { 0: '本周', [-1]: '上周', [-2]: '前两周' }
const gapWeek = ref<WeekOffset>(0)
const gap = ref<KnowledgeGapData | null>(null)
const exporting = ref(false)
const exportNote = ref('')

onMounted(() => {
  void load()
})

async function load(): Promise<void> {
  gap.value = await getKnowledgeGap(gapWeek.value)
}

function switchWeek(value: WeekOffset): void {
  if (value === gapWeek.value) return
  gapWeek.value = value
  exportNote.value = ''
  void load()
}

/** top10：端点已按 count 降序取前 50，卡片只展示前 10 行（P10 卡片规格） */
const top10 = computed(() => gap.value?.items.slice(0, 10) ?? [])

/**
 * 导出 CSV（F-1010）：当前周口径与卡片一致；blob → a[download] 触发；
 * X-Truncated: true 提示行数超上限已截断（内存放大保护，行数上限为服务端配置）。
 */
async function exportCsv(): Promise<void> {
  if (exporting.value) return
  exporting.value = true
  exportNote.value = ''
  try {
    const result = await exportKnowledgeGap(gapWeek.value)
    const url = URL.createObjectURL(result.blob)
    const a = document.createElement('a')
    a.href = url
    a.download = result.filename
    a.click()
    URL.revokeObjectURL(url)
    if (result.truncated) {
      exportNote.value = '结果行数超出导出上限，文件已截断（仅含头部条目）'
    }
  } catch (e) {
    exportNote.value = e instanceof ApiClientError ? e.message : '系统繁忙，请稍后重试'
  } finally {
    exporting.value = false
  }
}
</script>

<template>
  <section class="panel" data-test="gap-panel">
    <div class="gap-head">
      <h4>知识缺口 TOP（{{ GAP_WEEK_LABELS[gapWeek] }}，按被拒答次数降序）</h4>
      <div class="gap-tools">
        <el-select
          :model-value="gapWeek"
          size="small"
          style="width: 100px"
          data-test="gap-week-select"
          @change="switchWeek($event as WeekOffset)"
        >
          <el-option label="本周" :value="0" />
          <el-option label="上周" :value="-1" />
          <el-option label="前两周" :value="-2" />
        </el-select>
        <el-button size="small" :loading="exporting" data-test="gap-export-btn" @click="exportCsv"
          >导出 CSV</el-button
        >
      </div>
    </div>
    <el-table v-if="gap" :data="top10" border size="small">
      <el-table-column prop="question" label="问题" min-width="240" show-overflow-tooltip />
      <el-table-column prop="count" label="出现次数" width="100" />
      <template #empty>
        <el-empty description="暂无拒答——运行评测或等待用户提问" :image-size="60" />
      </template>
    </el-table>
    <p v-if="gap && gap.total > top10.length" class="gap-more">
      共 {{ gap.total }} 个去重问题，仅展示前 {{ top10.length }} 个，导出 CSV 可查看全部
    </p>
    <el-alert
      v-if="exportNote"
      :title="exportNote"
      type="warning"
      role="alert"
      show-icon
      :closable="true"
      class="gap-note"
      data-test="gap-export-note"
      @close="exportNote = ''"
    />
  </section>
</template>

<style scoped>
.panel {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 14px;
  margin-bottom: 16px;
}
.gap-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.gap-head h4 {
  margin: 0;
  font-size: 14px;
}
.gap-tools {
  display: flex;
  align-items: center;
  gap: 8px;
}
.gap-more {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.gap-note {
  margin-top: 10px;
}
</style>
