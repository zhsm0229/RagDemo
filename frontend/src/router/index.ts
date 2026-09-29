/**
 * 路由表：P1-P11 共 11 页（ui-spec §一权限矩阵）+ 首登改密页。
 * 页面组件均为占位（"建设中"空态），T7-T13 逐页替换实现。
 */
import { createRouter, createWebHistory } from 'vue-router'
import MainLayout from '@/layout/MainLayout.vue'
import { resolveGuard } from '@/router/guards'
import { useAuthStore } from '@/stores/auth'

import LoginView from '@/views/LoginView.vue'
import ChangePasswordView from '@/views/ChangePasswordView.vue'
import ChatView from '@/views/ChatView.vue'
import DocumentsView from '@/views/DocumentsView.vue'
import QaPairsView from '@/views/QaPairsView.vue'
import RetrievalTestView from '@/views/RetrievalTestView.vue'
import UsersView from '@/views/UsersView.vue'
import MessagesView from '@/views/MessagesView.vue'
import BadcaseView from '@/views/BadcaseView.vue'
import EvalView from '@/views/EvalView.vue'
import OverviewView from '@/views/OverviewView.vue'
import AuditView from '@/views/AuditView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: { name: 'chat' } },
    { path: '/login', name: 'login', component: LoginView },
    { path: '/change-password', name: 'change-password', component: ChangePasswordView },
    {
      path: '/',
      component: MainLayout,
      children: [
        { path: 'chat', name: 'chat', component: ChatView, meta: { title: '智能问答' } },
        {
          path: 'kb/documents',
          name: 'kb-documents',
          component: DocumentsView,
          meta: { title: '知识库·文档管理', requiresAdmin: true },
        },
        {
          path: 'kb/qa-pairs',
          name: 'kb-qa-pairs',
          component: QaPairsView,
          meta: { title: '知识库·问答对管理', requiresAdmin: true },
        },
        {
          path: 'admin/retrieval-test',
          name: 'admin-retrieval-test',
          component: RetrievalTestView,
          meta: { title: '检索测试', requiresAdmin: true },
        },
        {
          path: 'admin/users',
          name: 'admin-users',
          component: UsersView,
          meta: { title: '用户管理', requiresAdmin: true },
        },
        {
          path: 'admin/messages',
          name: 'admin-messages',
          component: MessagesView,
          meta: { title: '提问记录', requiresAdmin: true },
        },
        {
          path: 'admin/badcase',
          name: 'admin-badcase',
          component: BadcaseView,
          meta: { title: 'badcase 管理', requiresAdmin: true },
        },
        {
          path: 'admin/eval',
          name: 'admin-eval',
          component: EvalView,
          meta: { title: '评测管理', requiresAdmin: true },
        },
        {
          path: 'admin/overview',
          name: 'admin-overview',
          component: OverviewView,
          meta: { title: '运营概览', requiresAdmin: true },
        },
        {
          path: 'admin/audit',
          name: 'admin-audit',
          component: AuditView,
          meta: { title: '审计日志', requiresAdmin: true },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: { name: 'chat' } },
  ],
})

// 体验层守卫：me 只探一次（ensureLoaded），决策纯函数在 guards.ts（可单测）
router.beforeEach(async (to) => {
  const auth = useAuthStore()
  await auth.ensureLoaded()
  return resolveGuard(to, {
    isAuthenticated: auth.isAuthenticated,
    isAdmin: auth.isAdmin,
    mustChangePassword: auth.mustChangePassword,
  })
})

export default router
