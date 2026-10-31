<template>
  <!--
    列表/面板加载失败可重试态（B11 范式公共化，工单 08）：失败 ≠ 没有数据——
    错误态必须先于空态呈现"点击重试"。样式自 DocumentsView 提取（danger 色 +
    cursor pointer，整块可点）。data-test 由父组件传入（惯例 <slug>-load-error）。
  -->
  <div class="load-error" :data-test="dataTest" role="alert" @click="$emit('retry')">
    加载失败，点击重试
  </div>
</template>

<script setup lang="ts">
defineProps<{ dataTest: string }>()
defineEmits<{ retry: [] }>()
</script>

<style scoped>
.load-error {
  padding: 24px 0;
  text-align: center;
  color: var(--el-color-danger);
  cursor: pointer;
  font-size: 14px;
}
</style>
