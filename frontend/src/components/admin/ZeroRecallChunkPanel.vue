<script setup lang="ts">
/**
 * 零召回块清单折叠面板（W5 F-1011，P5 检索测试页内块级下钻）：近 30 天 0 命中的
 * 活跃块清单，与健康度零召回文档榜（F-1009）为同一口径的块级一面（服务端判定，
 * 见 services.overview_views.list_zero_recall_chunks docstring 互指）。首展懒加载
 * （收起不发请求，展开收起不重拉已载数据），翻页重拉；空清单出空态引导（§8.5）。
 * 独立成组件：检索测试视图守 300 行红线（AGENTS.md），本面板与调参结果互不依赖。
 */
import { ref } from 'vue'
import { ApiClientError } from '@/api/client'
import { listZeroRecallChunks, type ZeroRecallChunkItem } from '@/api/retrievalTest'
import { deptCodeLabel } from '@/components/admin/options'

const PAGE_SIZE = 10
const loading = ref(false)
const error = ref('')
const loaded = ref(false)
const items = ref<ZeroRecallChunkItem[]>([])
const total = ref(0)
const page = ref(1)

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const result = await listZeroRecallChunks({
      days: 30,
      page: page.value,
      pageSize: PAGE_SIZE,
    })
    items.value = result.items
    total.value = result.total
  } catch (e) {
    error.value = e instanceof ApiClientError ? e.message : '系统繁忙，请稍后重试'
  } finally {
    loading.value = false
  }
}

function onChange(names: Array<string | number>): void {
  // 仅首展触发懒加载；收起保留数据，翻页走分页回调
  if (names.length > 0 && !loaded.value) {
    loaded.value = true
    void load()
  }
}

function onPageChange(next: number): void {
  page.value = next
  void load()
}

/** ISO8601 → YYYY-MM-DD HH:mm（同 OverviewView/AuditView 口径） */
function fmtTime(iso: string): string {
  return iso.replace('T', ' ').slice(0, 16)
}
</script>

<template>
  <el-collapse @change="onChange">
    <el-collapse-item
      title="零召回块清单（F-1011 · 近 30 天 0 命中的活跃块，与健康度榜同口径）"
      name="zrc"
    >
      <div v-if="error" class="err-inline" role="alert">{{ error }}</div>
      <el-table v-if="items.length > 0" v-loading="loading" :data="items" border size="small">
        <el-table-column label="文档" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">{{ row.documentTitle }}</template>
        </el-table-column>
        <el-table-column label="部门" width="80">
          <template #default="{ row }">{{ deptCodeLabel(row.deptCode) }}</template>
        </el-table-column>
        <el-table-column label="标题路径" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">{{ row.titlePath ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="内容预览" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ row.contentPreview }}</template>
        </el-table-column>
        <el-table-column label="建档时间" width="150">
          <template #default="{ row }">{{ fmtTime(row.createdAt) }}</template>
        </el-table-column>
      </el-table>
      <el-empty v-else-if="loaded && !loading" description="窗口内没有零召回块" :image-size="80">
        <span class="empty-hint">所有活跃块近期均有命中，语料召回健康</span>
      </el-empty>
      <div v-if="total > PAGE_SIZE" class="zrc-pager">
        <el-pagination
          layout="prev, pager, next"
          :total="total"
          :page-size="PAGE_SIZE"
          :current-page="page"
          @current-change="onPageChange"
        />
      </div>
    </el-collapse-item>
  </el-collapse>
</template>

<style scoped>
.zrc-pager {
  margin-top: 10px;
  display: flex;
  justify-content: flex-end;
}
.empty-hint {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.err-inline {
  color: var(--el-color-danger);
  font-size: 12px;
  margin-bottom: 4px;
}
</style>
