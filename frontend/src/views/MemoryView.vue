<script setup lang="ts">
/**
 * 我的记忆（agent-ui-design §2，F-210 管理面）：开关置顶行（总闸语义首屏可读，
 * el-switch + 常驻说明，加载前 disabled 防"先渲染默认值再跳变"）+ 工具栏行
 * （左筛选 关键字+查询/重置 同行 §22.1；右手动添加 1-200 字，Enter 提交带 IME
 * 守卫，开关关闭时入口禁用 tooltip）+ 表格（内容/来源/添加时间/操作）+ 分页右下。
 *
 * 交互红线：开关切换即保存（v-model 先行更新，失败回滚，client 统一 toast；在飞锁
 * 防并发保存 B7）；删除统一 ElMessageBox.confirm（§2.6，文案面向员工不出现"软删"
 * 术语；行级在飞锁防连点 B8）；空态双分支（§2.5 未积累 vs 筛选无结果）+ 加载失败
 * 可重试态（B11）；关闭开关出现冻结说明 alert（§2.7），列表不隐藏不置灰——传达
 * "冻结而非删除"。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { InputInstance } from 'element-plus'
import {
  addMemory,
  deleteMemory,
  listMemories,
  updateMemorySettings,
  type MemoryItem,
} from '@/api/memory'
import LoadErrorState from '@/components/common/LoadErrorState.vue'
import { useAuthStore } from '@/stores/auth'

const CONTENT_MAX = 200 // 与后端 add_manual / user_memories_content_check 同源

const auth = useAuthStore()
// 开关初值来自 /auth/me 的 memoryEnabled（路由守卫已 ensureLoaded）；未探明前置
// disabled，避免先渲染默认值再跳变（对齐"禁止展示旧数据"红线）
const enabled = ref(auth.user?.memoryEnabled ?? false)
const settingsLoaded = computed(() => auth.user !== null)
const frozenAlertVisible = ref(false)

const filters = reactive({ keyword: '' })
const items = ref<MemoryItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
/** 加载失败标记（B11）：失败 ≠ 没有数据，空态区呈现"可重试"而非"还没有" */
const loadError = ref(false)

const draft = ref('')
const draftInputRef = ref<InputInstance>()
const trimmed = computed(() => draft.value.trim())
const overLimit = computed(() => trimmed.value.length > CONTENT_MAX)
const canAdd = computed(() => enabled.value && trimmed.value.length > 0 && !overLimit.value)
// 空态双分支判定：任一筛选条件非空即"筛选无结果"（§2.5）
const hasFilter = computed(() => filters.keyword.trim() !== '')

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await listMemories({
      keyword: filters.keyword,
      page: page.value,
      pageSize: pageSize.value,
    })
    items.value = data.items
    total.value = data.total
    loadError.value = false
  } catch {
    // client 已统一 toast；此处标错误态供空态区呈现"失败可重试"（B11）
    loadError.value = true
  } finally {
    loading.value = false
  }
}

onMounted(() => void load())

function search(): void {
  page.value = 1
  void load()
}

function resetFilters(): void {
  filters.keyword = ''
  search()
}

function onPageChange(next: number): void {
  page.value = next
  void load()
}

function onSizeChange(size: number): void {
  pageSize.value = size
  page.value = 1
  void load()
}

/** 开关切换即保存（§2.2）：v-model 先行更新，失败回滚；成功以后端落库值校准。
 * 在飞锁（B7）：请求挂起期间禁用开关并吞掉重复触发，防并发保存互相覆盖。 */
const toggling = ref(false)

async function onToggleSettings(): Promise<void> {
  if (toggling.value) return
  toggling.value = true
  try {
    const data = await updateMemorySettings(enabled.value)
    enabled.value = data.memoryEnabled
  } catch {
    enabled.value = !enabled.value
    return
  } finally {
    toggling.value = false
  }
  frozenAlertVisible.value = !enabled.value // 关闭瞬间出现冻结说明（§2.7）
  ElMessage.success(
    enabled.value
      ? '已开启：回答将参考你的记忆，系统也会在问答中提炼新记忆'
      : '已关闭：回答不再参考你的记忆，也不再自动提炼；已有记忆保留',
  )
}

/** 手动添加（§2.3）：strip 口径 1-200 字；成功清空 + 回第 1 页刷新（新条目首行可见）。
 * 在飞锁（B7）：提交期间按钮 loading 并吞掉重复提交；超限给出明确警示而非静默吞掉。 */
const adding = ref(false)

async function submitDraft(): Promise<void> {
  if (!enabled.value || adding.value) return
  if (overLimit.value) {
    ElMessage.warning('记忆内容不能超过 200 字')
    return
  }
  if (trimmed.value.length === 0) return
  adding.value = true
  try {
    await addMemory(trimmed.value)
    draft.value = ''
    ElMessage.success('已添加')
    page.value = 1
    void load()
  } finally {
    adding.value = false
  }
}

function onEnter(event: KeyboardEvent): void {
  // 中文输入法选词回车不发送（isComposing / keyCode 229 守卫，同 ChatInput）
  if (event.isComposing || event.keyCode === 229) return
  void submitDraft()
}

/** 删除确认（§2.6）：统一 ElMessageBox，文案面向员工不出现内部术语。
 * 行级在飞锁（B8）：删除确认挂起/请求在飞期间吞掉重复点击，防连点重复删除。 */
const deletingId = ref<number | null>(null)

async function removeRow(row: MemoryItem): Promise<void> {
  if (deletingId.value !== null) return
  deletingId.value = row.id
  try {
    try {
      await ElMessageBox.confirm('删除后，回答将不再参考这条记忆，确认删除？', '删除记忆', {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
      })
    } catch {
      return // 取消归取消，不发请求
    }
    await deleteMemory(row.id)
    ElMessage.success('已删除')
    // §22.3：保持当前页刷新，当前页被删空时回退一页
    if (items.value.length <= 1 && page.value > 1) page.value -= 1
    void load()
  } finally {
    deletingId.value = null
  }
}

/** 空态引导（§2.5）：未积累 → 聚焦添加输入框；开关关闭时不渲染该按钮（不引导进死胡同） */
function focusDraft(): void {
  draftInputRef.value?.focus()
}

/** 加载失败重试（B11）：保留当前筛选与分页原地重拉 */
function retryLoad(): void {
  void load()
}

/** DTO createdAt 为 ISO 字符串 → "YYYY-MM-DD HH:mm"（同 QaPairsView 口径） */
function formatTime(iso: string): string {
  return iso.replace('T', ' ').slice(0, 16)
}
</script>

<template>
  <div>
    <!-- 开关置顶行（§2.1）：总闸语义首屏可读，常驻说明不折叠、不放 tooltip -->
    <div class="switch-row" data-test="memory-switch-row">
      <span class="switch-label">记忆功能</span>
      <el-switch
        v-model="enabled"
        :disabled="!settingsLoaded || toggling"
        data-test="memory-switch"
        @change="onToggleSettings"
      />
      <span class="switch-hint">
        开启后，回答会参考你的记忆；关闭后立即退出参考，已有记忆保留、不自动删除。
      </span>
    </div>
    <el-alert
      v-if="frozenAlertVisible && !enabled"
      class="frozen-alert"
      type="info"
      :closable="true"
      title="记忆功能已关闭：回答不再参考你的记忆，系统不再自动提炼；列表仅供查看与清理，开启后恢复。"
      data-test="memory-frozen-alert"
      show-icon
    />

    <!-- 工具栏行（§2.1）：左筛选（变更即查回第 1 页），右手动添加 -->
    <div class="toolbar-row">
      <el-input
        v-model="filters.keyword"
        placeholder="记忆内容关键字"
        style="width: 200px"
        clearable
        data-test="memory-keyword"
        @keyup.enter="search"
        @clear="search"
      />
      <el-button type="primary" data-test="memory-search" @click="search">查询</el-button>
      <el-button data-test="memory-reset" @click="resetFilters">重置</el-button>
      <span class="spacer"></span>
      <!-- 开关关闭时添加入口整体禁用：禁用控件外包 span 使 tooltip 生效（ChatInput 先例） -->
      <el-tooltip content="开启记忆后可手动添加" :disabled="enabled" placement="top">
        <span class="add-wrap">
          <el-input
            ref="draftInputRef"
            v-model="draft"
            :disabled="!enabled"
            :maxlength="CONTENT_MAX + 20"
            placeholder="手动添加一条记忆（1-200 字）"
            style="width: 260px"
            data-test="memory-draft"
            @keydown.enter="onEnter"
          />
          <span class="draft-counter" :class="{ over: overLimit }">
            {{ trimmed.length }}/{{ CONTENT_MAX }}
          </span>
          <el-button
            type="primary"
            :disabled="!canAdd"
            :loading="adding"
            data-test="memory-add"
            @click="submitDraft"
          >
            添加
          </el-button>
        </span>
      </el-tooltip>
    </div>

    <el-table v-loading="loading" :data="items" border stripe data-test="memory-list">
      <el-table-column label="内容" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">{{ row.content }}</template>
      </el-table-column>
      <el-table-column label="来源" width="90">
        <template #default="{ row }">
          <el-tooltip
            :content="row.source === 'auto' ? '系统在问答中提炼' : '你手动添加'"
            placement="top"
          >
            <el-tag :type="row.source === 'auto' ? 'info' : 'success'" size="small">
              {{ row.source === 'auto' ? '自动' : '手动' }}
            </el-tag>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="添加时间" width="150">
        <template #default="{ row }">{{ formatTime(row.createdAt) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90">
        <template #default="{ row }">
          <el-button
            link
            type="danger"
            :disabled="deletingId !== null"
            data-test="memory-delete"
            @click="removeRow(row)"
          >
            删除
          </el-button>
        </template>
      </el-table-column>
      <!-- 空态三分支：加载失败可重试（B11）优先，其后筛选无结果 vs 未积累（§2.5） -->
      <template #empty>
        <LoadErrorState v-if="loadError" data-test="memory-load-error" @retry="retryLoad" />
        <el-empty
          v-else-if="hasFilter"
          description="没有匹配筛选条件的记忆，试试更换关键字"
          :image-size="60"
          data-test="memory-empty-filtered"
        >
          <el-button size="small" @click="resetFilters">清除筛选</el-button>
        </el-empty>
        <el-empty
          v-else
          description="还没有记忆（问答中提炼或手动添加后会出现在这里）"
          :image-size="60"
          data-test="memory-empty-blank"
        >
          <!-- 开关关闭时不渲染"手动添加一条"引导按钮：避免引导进死胡同 -->
          <el-button v-if="enabled" type="primary" size="small" @click="focusDraft">
            手动添加一条
          </el-button>
        </el-empty>
      </template>
    </el-table>

    <div class="pager-row">
      <el-pagination
        :current-page="page"
        :page-size="pageSize"
        :page-sizes="[10, 20, 50, 100]"
        :total="total"
        layout="total, sizes, prev, pager, next"
        @current-change="onPageChange"
        @size-change="onSizeChange"
      />
    </div>
  </div>
</template>

<style scoped>
/* 开关置顶行：总闸说明常驻可读（§2.1）；toolbar/pager 对照原型 §22.1 */
.switch-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.switch-label {
  font-weight: 600;
}
.switch-hint {
  color: var(--el-text-color-secondary);
}
.frozen-alert {
  margin-bottom: 12px;
}
.toolbar-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.spacer {
  flex: 1;
}
.add-wrap {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.draft-counter {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  min-width: 48px;
}
.draft-counter.over {
  color: var(--el-color-danger);
}
.pager-row {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
</style>
