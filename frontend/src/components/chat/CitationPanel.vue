<script setup lang="ts">
/**
 * 引用面板（P2 右栏，对照原型 cite-panel/cite-card，可折叠）：
 * 展示最近一条 assistant 回答的引用（citations 事件先于 token 到达即上板），
 * 角标点击联动高亮对应卡片；"查看原文"核对弹窗与点踩反馈属 T9 交付范围。
 */
import type { CitationItem } from '@/api/types'

defineProps<{
  citations: CitationItem[]
  /** 当前高亮的引用编号（气泡角标点击联动） */
  highlightN: number | null
}>()

defineEmits<{
  close: []
}>()
</script>

<template>
  <div class="cite-panel">
    <div class="head">
      <span>引用来源</span>
      <span class="spacer" />
      <el-button link size="small" @click="$emit('close')">收起</el-button>
    </div>
    <div class="list">
      <el-empty
        v-if="citations.length === 0"
        description="无引用（拒答回答不带引用）"
        :image-size="60"
      />
      <div
        v-for="c in citations"
        :id="`cite-card-${c.n}`"
        :key="c.n"
        class="cite-card"
        :class="{ hl: highlightN === c.n }"
      >
        <div class="cite-title">
          <span class="cite-name">[{{ c.n }}] {{ c.title }}</span>
          <el-tag size="small" :type="c.refType === 'qa' ? 'warning' : 'primary'">
            {{ c.refType === 'qa' ? '问答对' : '文档块' }}
          </el-tag>
        </div>
        <div class="score">
          {{ c.titlePath || '—' }} · 相似度 {{ c.score }}
          <template v-if="c.page !== null"> · 第 {{ c.page }} 页</template>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cite-panel {
  width: 300px;
  background: #fff;
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}
.head {
  padding: 10px 14px;
  border-bottom: 1px solid var(--el-border-color-extra-light);
  font-weight: 600;
  display: flex;
  align-items: center;
}
.spacer {
  flex: 1;
}
.list {
  flex: 1;
  overflow: auto;
  padding: 10px;
}
.cite-card {
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  padding: 10px;
  margin-bottom: 8px;
  font-size: 13px;
  transition: all 0.2s;
}
.cite-card.hl,
.cite-card:hover {
  border-color: var(--el-color-primary);
}
.cite-card.hl {
  background: var(--el-color-primary-light-9);
}
.cite-title {
  display: flex;
  align-items: center;
  gap: 6px;
}
.cite-name {
  font-weight: 600;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.score {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin-top: 4px;
}
</style>
