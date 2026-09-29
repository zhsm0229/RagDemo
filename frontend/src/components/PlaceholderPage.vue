<script setup lang="ts">
/**
 * 占位页统一空态（standards §8.5：空状态必须给出下一步引导）。
 * T7-T13 实装时整文件替换各视图，本组件只服务骨架期。
 */
defineProps<{
  title: string
  /** 独立页（登录/改密）无侧边导航可引导，文案切换 */
  standalone?: boolean
}>()
</script>

<template>
  <div class="placeholder">
    <el-empty :description="`「${title}」页面建设中`">
      <div class="guide">
        <el-text type="info">
          本页面将在后续任务交付。{{
            standalone ? '请先使用系统已上线的功能。' : '可先通过左侧导航使用已上线的功能。'
          }}
        </el-text>
      </div>
    </el-empty>
  </div>
</template>

<style scoped>
.placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100%;
}
.guide {
  text-align: center;
}
</style>
