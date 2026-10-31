<script setup lang="ts">
/**
 * 全局布局（ui-spec §十）：侧边导航（member 仅见问答）+ 顶栏（用户名/角色/退出）+ 内容区。
 * 菜单经 el-menu 的 router 模式直接按 path 导航。
 * 侧栏折叠（工单 18，设计 18-ui-responsive-design §1）：aside 底部 toggle 切换，
 * 折叠态 64px 纯图标（#title 插槽随 EP 折叠模式自动转 popper tooltip）；
 * 状态持久化 localStorage（读写 try/catch 容错，缺省展开）；宽度过渡 .2s，
 * el-menu 自带 collapse 动画关闭，两套动画叠加会抖动。
 */
import { computed, ref, type Component } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ChatDotRound,
  ChatLineSquare,
  Document,
  DocumentCopy,
  Expand,
  Fold,
  InfoFilled,
  MagicStick,
  Notebook,
  Search,
  Stopwatch,
  Tickets,
  TrendCharts,
  User,
  Warning,
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'

interface MenuItem {
  path: string
  label: string
  /** 折叠态唯一视觉锚点：collapse 模式只剩图标，无图标则 64px 空白块 */
  icon: Component
}

interface MenuGroup {
  title: string
  items: MenuItem[]
}

// 侧栏折叠持久化（全站首个 localStorage 使用点，值 '1'/'0'）：读写均容错——
// 隐私模式/存储禁用时 setItem 抛异常不致命，仅本次会话不记忆；非法值解析失败回缺省展开
const SIDEBAR_KEY = 'rag.ui.sidebarCollapsed'

function readSidebarCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_KEY) === '1'
  } catch {
    return false
  }
}

function writeSidebarCollapsed(value: boolean): void {
  try {
    localStorage.setItem(SIDEBAR_KEY, value ? '1' : '0')
  } catch {
    // 容错：不持久化，折叠状态仅当前会话有效
  }
}

const collapsed = ref(readSidebarCollapsed())

function onToggleSidebar(): void {
  collapsed.value = !collapsed.value
  writeSidebarCollapsed(collapsed.value)
}

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

// 权限矩阵（ui-spec §一 + agent-ui-design §1）：member 可问答/我的记忆/帮助；
// 记忆是个人数据与问答同属员工日常动线（不放"知识库/管理"两组）；知识库/管理两组 admin 专属
const menuGroups = computed<MenuGroup[]>(() => {
  const groups: MenuGroup[] = [
    {
      title: '',
      items: [
        { path: '/chat', label: '智能问答', icon: ChatDotRound },
        { path: '/memory', label: '我的记忆', icon: Notebook },
        { path: '/help', label: '帮助', icon: InfoFilled },
      ],
    },
  ]
  if (auth.isAdmin) {
    groups.push({
      title: '知识库',
      items: [
        { path: '/kb/documents', label: '文档管理', icon: Document },
        { path: '/kb/qa-pairs', label: '问答对管理', icon: ChatLineSquare },
        // 模板生成（agent-ui-design §1.1）：仅 admin，产物草稿流向"问答对管理"审核
        { path: '/kb/template-gen', label: '模板生成', icon: MagicStick },
      ],
    })
    groups.push({
      title: '管理',
      items: [
        { path: '/admin/retrieval-test', label: '检索测试', icon: Search },
        { path: '/admin/users', label: '用户管理', icon: User },
        { path: '/admin/messages', label: '提问记录', icon: Tickets },
        { path: '/admin/badcase', label: '问题反馈管理', icon: Warning },
        { path: '/admin/eval', label: '评测管理', icon: Stopwatch },
        { path: '/admin/overview', label: '运营概览', icon: TrendCharts },
        { path: '/admin/audit', label: '审计日志', icon: DocumentCopy },
      ],
    })
  }
  return groups
})

const activePath = computed(() => route.path)

function onLogout(): void {
  auth.logout()
  ElMessage.success('已退出登录')
  router.push({ name: 'login' })
}
</script>

<template>
  <el-container class="layout">
    <el-aside
      :width="collapsed ? '64px' : '220px'"
      class="aside"
      :class="{ 'is-collapsed': collapsed }"
    >
      <div v-if="!collapsed" class="brand">企业智能工作台</div>
      <div v-else class="brand brand--mini">企</div>
      <el-menu
        :default-active="activePath"
        router
        class="menu"
        :collapse="collapsed"
        :collapse-transition="false"
      >
        <template v-for="group in menuGroups" :key="group.title || 'main'">
          <el-menu-item-group v-if="group.title" :title="group.title">
            <el-menu-item v-for="item in group.items" :key="item.path" :index="item.path">
              <el-icon><component :is="item.icon" /></el-icon>
              <template #title>{{ item.label }}</template>
            </el-menu-item>
          </el-menu-item-group>
          <template v-else>
            <el-menu-item v-for="item in group.items" :key="item.path" :index="item.path">
              <el-icon><component :is="item.icon" /></el-icon>
              <template #title>{{ item.label }}</template>
            </el-menu-item>
          </template>
        </template>
      </el-menu>
      <div class="sidebar-footer">
        <el-button
          link
          class="toggle-btn"
          data-test="sidebar-toggle"
          :aria-label="collapsed ? '展开侧栏' : '折叠侧栏'"
          :aria-expanded="collapsed ? 'false' : 'true'"
          @click="onToggleSidebar"
        >
          <el-icon :size="18"><component :is="collapsed ? Expand : Fold" /></el-icon>
        </el-button>
      </div>
    </el-aside>

    <el-container>
      <el-header class="header" height="56px">
        <span class="page-title">{{ route.meta.title || '智能问答' }}</span>
        <span class="spacer" />
        <el-tag v-if="auth.isAdmin" size="small" type="warning">管理员</el-tag>
        <span class="username">{{ auth.user?.username }}</span>
        <el-button link type="danger" @click="onLogout">退出</el-button>
      </el-header>
      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.layout {
  height: 100vh;
}
.aside {
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--el-border-color-light);
  transition: width 0.2s;
  overflow: hidden;
}
.brand {
  font-weight: 600;
  font-size: 16px;
  padding: 18px 20px;
  color: var(--el-color-primary);
  white-space: nowrap;
}
.aside.is-collapsed .brand--mini {
  padding: 18px 0;
  text-align: center;
}
.menu {
  border-right: none;
  flex: 1;
  overflow-y: auto;
}
/* EP 2.14 折叠态不自动隐藏 group 标题（组件恒渲染 __title div），64px 下折行难看，手动隐藏 */
.menu.el-menu--collapse :deep(.el-menu-item-group__title) {
  display: none;
}
.sidebar-footer {
  display: flex;
  justify-content: flex-end;
  padding: 6px 10px;
  border-top: 1px solid var(--el-border-color-light);
}
.aside.is-collapsed .sidebar-footer {
  justify-content: center;
  padding: 6px 0;
}
.header {
  display: flex;
  align-items: center;
  gap: 12px;
  border-bottom: 1px solid var(--el-border-color-light);
}
.page-title {
  font-weight: 600;
}
.spacer {
  flex: 1;
}
.username {
  color: var(--el-text-color-regular);
}
/* 工单 18 窄屏兜底：主容器最小宽，el-main 自带 overflow:auto，横向滚动收在内容区内 */
.main {
  min-width: 768px;
}
</style>
