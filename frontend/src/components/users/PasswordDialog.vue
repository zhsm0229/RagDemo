<script setup lang="ts">
/**
 * 初始密码一次性展示对话框（F-102 验收，§8.8 只读置灰可复制）：
 * 明文仅存在于父级发放/重置响应的那一次，无右上角关闭、禁遮罩关闭（§22.2），
 * 唯一出口"我已保存"——关闭后父级弃明文，不可再看。
 */
import { ElMessage } from 'element-plus'

const props = defineProps<{ visible: boolean; password: string }>()
const emit = defineEmits<{ close: [] }>()

/** 复制初始密码：Clipboard API 不可用（http 部署/旧浏览器）时提示手动选中复制 */
async function copyPwd(): Promise<void> {
  if (typeof navigator.clipboard?.writeText === 'function') {
    await navigator.clipboard.writeText(props.password)
    ElMessage.success('已复制')
  } else {
    ElMessage.info('请手动选中复制')
  }
}
</script>

<template>
  <el-dialog
    :model-value="visible"
    title="初始密码（仅此一次展示）"
    width="440px"
    :close-on-click-modal="false"
    :show-close="false"
    @update:model-value="emit('close')"
  >
    <el-alert
      type="warning"
      :closable="false"
      show-icon
      title="请立即复制并发送给用户，关闭后不可再查看；用户首次登录将被强制改密"
      class="warn"
    />
    <div class="readonly-grey">
      <el-input :model-value="password" readonly class="pwd-input" aria-label="初始密码">
        <template #append>
          <el-button @click="copyPwd">复制</el-button>
        </template>
      </el-input>
    </div>
    <template #footer>
      <el-button type="primary" @click="emit('close')">我已保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.warn {
  margin-bottom: 12px;
}
/* §8.8 只读置灰：值可见可选中复制，不可编辑感 */
.readonly-grey :deep(.el-input__inner) {
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-light);
  cursor: text;
}
</style>
