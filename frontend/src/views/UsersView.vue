<script setup lang="ts">
/**
 * P6 用户管理（ui-spec §六，对照原型 UsersView，F-102）：
 * 筛选行（用户名关键字 + 查询/重置——§22.1 文本型条件必须配按钮）+ 工具栏
 * （新建用户）+ 表格（用户名/姓名/部门/角色/状态/最近登录/操作）+ 分页
 * （10/20/50/100 默认 20 右下）。发放账号 → 201 后初始密码一次性展示
 * （PasswordDialog §8.8 只读可复制，关闭即弃）；禁用/启用 popconfirm，禁用自己
 * 后端 422 → 表格上方内联（§22.2）；重置密码 popconfirm → 同一次性展示。
 * 原型第三态"解锁"操作无后端契约（锁定由重置密码清除或自然过期）→ 不渲染
 * （deviations #37）；mustChangePassword 不单独成列（原型无此列，重置即置位）。
 * 空态双分支（B9，照 MemoryView §2.5 范式）：筛选无结果 vs 未积累。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  listAdminUsers,
  resetAdminUserPassword,
  toggleAdminUserStatus,
  type AdminUserItem,
  type InitialPasswordResult,
} from '@/api/adminUsers'
import { ApiClientError } from '@/api/client'
import { deptCodeLabel } from '@/components/admin/options'
import LoadErrorState from '@/components/common/LoadErrorState.vue'
import CreateUserDialog from '@/components/users/CreateUserDialog.vue'
import PasswordDialog from '@/components/users/PasswordDialog.vue'

const filters = reactive({ keyword: '' })
const items = ref<AdminUserItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
/** 加载失败标记（B11/工单 08）：失败 ≠ 没有数据，空态区呈现"可重试"而非"还没有" */
const loadError = ref(false)
const createVisible = ref(false)
const pwdVisible = ref(false)
/** 一次性明文：仅发放/重置响应带来的那一份，关弹窗即弃 */
const pwdValue = ref('')
/** 行级操作错误（禁用自己 422 等）内联于表格上方（§22.2） */
const actionErr = ref('')

// 空态双分支判定（B9）：关键字筛选非空即"筛选无结果"
const hasFilter = computed(() => filters.keyword.trim() !== '')

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await listAdminUsers({
      keyword: filters.keyword,
      page: page.value,
      pageSize: pageSize.value,
    })
    items.value = data.items
    total.value = data.total
    loadError.value = false
  } catch {
    // client 已统一 toast；此处标错误态供空态区呈现"失败可重试"（B11/工单 08）
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

/** 状态 tag：后端 status（正常/已禁用）+ locked 派生第三态（原型"锁定至xx"无时刻字段） */
function statusTag(row: AdminUserItem): { text: string; type: 'success' | 'danger' | 'warning' } {
  if (row.status !== '正常') return { text: row.status, type: 'danger' }
  if (row.locked) return { text: '已锁定', type: 'warning' }
  return { text: '正常', type: 'success' }
}

function toggleTitle(row: AdminUserItem): string {
  return row.status === '正常' ? '禁用后该账号立即失效，确认？' : '确认恢复该账号？'
}

async function toggleActive(row: AdminUserItem): Promise<void> {
  const disable = row.status === '正常'
  actionErr.value = ''
  try {
    await toggleAdminUserStatus(row.id, !disable)
  } catch (error) {
    actionErr.value = error instanceof ApiClientError ? error.message : '系统繁忙，请稍后重试'
    return
  }
  ElMessage.success(disable ? '已禁用：该账号立即无法登录' : '已启用，可正常登录')
  void load()
}

async function resetPwd(row: AdminUserItem): Promise<void> {
  actionErr.value = ''
  try {
    showPassword(await resetAdminUserPassword(row.id))
  } catch (error) {
    actionErr.value = error instanceof ApiClientError ? error.message : '系统繁忙，请稍后重试'
  }
}

function showPassword(result: InitialPasswordResult): void {
  pwdValue.value = result.initialPassword
  pwdVisible.value = true
}

function onPwdClosed(): void {
  pwdVisible.value = false
  pwdValue.value = '' // 一次性语义：关闭即弃明文
}

/** 发放成功：关弹窗 → 初始密码展示 → 回第 1 页刷新（§22.3 新增立即可见） */
function onCreated(result: InitialPasswordResult): void {
  createVisible.value = false
  showPassword(result)
  page.value = 1
  void load()
}

/** DTO lastLoginAt 为 ISO 字符串或 null → 原型口径 "YYYY-MM-DD HH:mm" 或 "-" */
function formatTime(iso: string | null): string {
  return iso === null ? '-' : iso.replace('T', ' ').slice(0, 16)
}
</script>

<template>
  <div>
    <div class="filter-row">
      <el-input
        v-model="filters.keyword"
        placeholder="搜索用户名"
        style="width: 200px"
        clearable
        @keyup.enter="search"
        @clear="search"
      />
      <el-button type="primary" @click="search">查询</el-button>
      <el-button @click="resetFilters">重置</el-button>
      <span class="spacer"></span>
      <el-button type="primary" @click="createVisible = true">＋ 新建用户</el-button>
    </div>

    <el-alert
      v-if="actionErr"
      :title="actionErr"
      type="error"
      role="alert"
      show-icon
      closable
      class="action-err"
      @close="actionErr = ''"
    />

    <el-table v-loading="loading" :data="items" border stripe>
      <el-table-column prop="username" label="用户名" width="140" />
      <el-table-column prop="name" label="姓名" width="110" />
      <el-table-column label="部门" width="110">
        <template #default="{ row }">{{ deptCodeLabel(row.deptCode) }}</template>
      </el-table-column>
      <el-table-column label="角色" width="100">
        <template #default="{ row }">
          <el-tag :type="row.role === 'admin' ? 'danger' : undefined" size="small">
            {{ row.role === 'admin' ? '管理员' : '成员' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="130">
        <template #default="{ row }">
          <el-tag :type="statusTag(row).type" size="small">{{ statusTag(row).text }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="最近登录" width="150">
        <template #default="{ row }">{{ formatTime(row.lastLoginAt) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-popconfirm :title="toggleTitle(row)" @confirm="toggleActive(row)">
            <template #reference>
              <el-button link :type="row.status === '正常' ? 'danger' : 'primary'">
                {{ row.status === '正常' ? '禁用' : '启用' }}
              </el-button>
            </template>
          </el-popconfirm>
          <el-popconfirm
            title="重置后将生成新的初始密码（一次性展示），确认？"
            @confirm="resetPwd(row)"
          >
            <template #reference>
              <el-button link type="warning">重置密码</el-button>
            </template>
          </el-popconfirm>
        </template>
      </el-table-column>
      <!-- 空态三分支（B11/工单 08 + B9）：加载失败可重试 > 筛选无结果 > 未积累 -->
      <template #empty>
        <LoadErrorState v-if="loadError" data-test="users-load-error" @retry="load" />
        <el-empty
          v-else-if="hasFilter"
          description="没有匹配筛选条件的用户"
          :image-size="60"
          data-test="users-empty-filtered"
        >
          <el-button size="small" @click="resetFilters">清除筛选</el-button>
        </el-empty>
        <el-empty v-else description="还没有用户" :image-size="60">
          <el-button type="primary" size="small" @click="createVisible = true">
            新建第一个用户
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

    <CreateUserDialog
      :visible="createVisible"
      @close="createVisible = false"
      @created="onCreated"
    />
    <PasswordDialog :visible="pwdVisible" :password="pwdValue" @close="onPwdClosed" />
  </div>
</template>

<style scoped>
/* 对照原型 .filter-row / .pager-row（§22.1 查询条件同行、分页右下） */
.filter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.pager-row {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
.spacer {
  flex: 1;
}
.action-err {
  margin-bottom: 12px;
}
</style>
