<script setup lang="ts">
/**
 * 跑分逐题卡片（api.md §4.2.5 明细项）：#题号 + 题型/部门 tag + 期望答案 + 回答
 * （只读置灰 §8.8）+ 命中引用折叠（JSONB 原样）+ 自动/人工判定 tag + 录分按钮。
 * 题面文本不在 detail DTO（题库无列表端点），以 #questionId + 期望答案呈现
 * （deviations #41）。单题失败渲染 error 提示（user 消息留痕、不落 assistant 行）。
 */
import type { EvalRunDetailItem } from '@/api/evalAdmin'
import { answerTypeLabel } from '@/components/admin/options'
import { evalDeptLabel, formatPercent, judgmentLabel, qtypeLabel } from './options'

defineProps<{
  item: EvalRunDetailItem
  /** 仅 status=done 可录分（running/failed 后端 422/3004） */
  judgeable: boolean
}>()
const emit = defineEmits<{ judge: [item: EvalRunDetailItem] }>()
</script>

<template>
  <div class="item-card" :data-test="`eval-item-${item.questionId}`">
    <div class="head">
      <el-tag size="small" class="gap">#{{ item.questionId }}</el-tag>
      <el-tag size="small" type="info" class="gap">{{ qtypeLabel(item.qtype) }}</el-tag>
      <el-tag size="small" type="info" class="gap">{{ evalDeptLabel(item.deptId) }}</el-tag>
      <span class="muted">回答类型：{{ answerTypeLabel(item.answerType || null) }}</span>
      <span class="spacer"></span>
      <el-tag
        size="small"
        :type="item.autoJudgment === 'correct' ? 'success' : 'warning'"
        class="gap"
      >
        自动：{{ judgmentLabel(item.autoJudgment) }}
      </el-tag>
      <el-tag
        v-if="item.judgment"
        size="small"
        :type="
          item.judgment === 'correct'
            ? 'success'
            : item.judgment === 'partial'
              ? 'warning'
              : 'danger'
        "
      >
        人工：{{ judgmentLabel(item.judgment) }}
      </el-tag>
      <el-tooltip v-if="judgeable" content="三档判定：正确/部分正确/错误" placement="top">
        <el-button link type="primary" @click="emit('judge', item)">评判</el-button>
      </el-tooltip>
    </div>
    <p class="line">期望答案：{{ item.expected }}</p>
    <el-input
      v-if="item.error === null"
      type="textarea"
      :rows="2"
      :model-value="item.answer || '（无回答内容）'"
      readonly
      class="answer"
    />
    <el-alert
      v-else
      type="error"
      :title="`单题执行失败：${item.error}`"
      :closable="false"
      class="err"
    />
    <el-collapse v-if="item.citations.length" class="cite">
      <el-collapse-item :title="`命中引用（${item.citations.length} 条）`">
        <pre class="cite-json">{{ JSON.stringify(item.citations, null, 2) }}</pre>
      </el-collapse-item>
    </el-collapse>
    <p v-if="item.citations.length" class="muted">
      引用最高分：{{ formatPercent(Math.max(...item.citations.map((c) => c.score))) }}
    </p>
  </div>
</template>

<style scoped>
.item-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 10px;
  margin-bottom: 10px;
}
.head {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}
.gap {
  margin-right: 4px;
}
.line {
  margin: 4px 0;
  font-size: 13px;
}
.answer {
  margin: 6px 0;
}
.err {
  margin: 6px 0;
}
.cite {
  border: none;
}
.cite-json {
  margin: 0;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}
.muted {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.spacer {
  flex: 1;
}
</style>
