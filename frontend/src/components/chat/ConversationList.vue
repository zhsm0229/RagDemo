<script setup lang="ts">
/**
 * 会话列表（P2 左栏，对照原型 conv-list/conv-item）：新建 / 删除入口（二次确认在
 * 页面层）/ 点击切换 / 活跃高亮 / 时间展示（相对口径归 utils/fmtTimeRelative）。
 */
import type { ConversationItem } from '@/api/chat'
import { fmtTimeRelative } from '@/utils/format'

defineProps<{
  items: ConversationItem[]
  activeId: number | null
}>()

const emit = defineEmits<{
  select: [id: number]
  create: []
  remove: [id: number]
}>()
</script>

<template>
  <div class="conv-list">
    <el-button type="primary" class="new-btn" @click="emit('create')">＋ 新会话</el-button>
    <div class="conv-items">
      <el-empty v-if="items.length === 0" description="还没有会话" :image-size="60">
        <el-button type="primary" size="small" @click="emit('create')">发起第一场问答</el-button>
      </el-empty>
      <div
        v-for="c in items"
        :key="c.id"
        class="conv-item"
        :class="{ active: c.id === activeId }"
        @click="emit('select', c.id)"
      >
        <div class="conv-text">
          <div class="conv-title">{{ c.title }}</div>
          <div class="conv-time">{{ fmtTimeRelative(c.updatedAt) }}</div>
        </div>
        <el-button class="del" link size="small" @click.stop="emit('remove', c.id)">删除</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.conv-list {
  width: 230px;
  background: #fff;
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}
.new-btn {
  margin: 10px;
  width: calc(100% - 20px);
}
.conv-items {
  flex: 1;
  overflow: auto;
}
.conv-item {
  padding: 10px 12px;
  cursor: pointer;
  border-bottom: 1px solid var(--el-border-color-extra-light);
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 6px;
}
.conv-item:hover {
  background: var(--el-fill-color-light);
}
.conv-item.active {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
}
.conv-text {
  min-width: 0;
}
.conv-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 14px;
}
.conv-time {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}
.del {
  visibility: hidden;
  flex-shrink: 0;
}
.conv-item:hover .del {
  visibility: visible;
}
</style>
