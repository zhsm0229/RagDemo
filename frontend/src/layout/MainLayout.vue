<script setup lang="ts">
/**
 * 全局布局（ui-spec §十）：侧边导航（member 仅见问答）+ 顶栏（用户名/角色/退出）+ 内容区。
 * 菜单经 el-menu 的 router 模式直接按 path 导航。
 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

interface MenuItem {
  path: string
  label: string
}

interface MenuGroup {
  title: string
  items: MenuItem[]
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
        { path: '/chat', label: '智能问答' },
        { path: '/memory', label: '我的记忆' },
        { path: '/help', label: '帮助' },
      ],
    },
  ]
  if (auth.isAdmin) {
    groups.push({
      title: '知识库',
      items: [
        { path: '/kb/documents', label: '文档管理' },
        { path: '/kb/qa-pairs', label: '问答对管理' },
        // 模板生成（agent-ui-design §1.1）：仅 admin，产物草稿流向"问答对管理"审核
        { path: '/kb/template-gen', label: '模板生成' },
      ],
    })
    groups.push({
      title: '管理',
      items: [
        { path: '/admin/retrieval-test', label: '检索测试' },
        { path: '/admin/users', label: '用户管理' },
        { path: '/admin/messages', label: '提问记录' },
        { path: '/admin/badcase', label: '问题反馈管理' },
        { path: '/admin/eval', label: '评测管理' },
        { path: '/admin/overview', label: '运营概览' },
        { path: '/admin/audit', label: '审计日志' },
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
    <el-aside width="220px" class="aside">
      <div class="brand">企业智能工作台</div>
      <el-menu :default-active="activePath" router class="menu">
        <template v-for="group in menuGroups" :key="group.title || 'main'">
          <el-menu-item-group v-if="group.title" :title="group.title">
            <el-menu-item v-for="item in group.items" :key="item.path" :index="item.path">
              {{ item.label }}
            </el-menu-item>
          </el-menu-item-group>
          <template v-else>
            <el-menu-item v-for="item in group.items" :key="item.path" :index="item.path">
              {{ item.label }}
            </el-menu-item>
          </template>
        </template>
      </el-menu>
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
  border-right: 1px solid var(--el-border-color-light);
}
.brand {
  font-weight: 600;
  font-size: 16px;
  padding: 18px 20px;
  color: var(--el-color-primary);
}
.menu {
  border-right: none;
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
</style>
