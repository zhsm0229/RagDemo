<script setup lang="ts">
/**
 * 帮助中心（傻瓜式在线帮助）：纯静态内容页，无 API 依赖、无新增第三方依赖。
 * 内容数据在 src/help/*（与 docs/user-guide.md 同源），渲染按 HelpBlock.kind 分发：
 * steps=编号步骤，tips=要点列表，table=对照表（列数不定故用原生表格），note=el-alert 提示。
 * adminOnly 页签对成员隐藏（与菜单权限矩阵同口径，ui-spec §一）。
 * 页签选中态同步 route.query.tab（M40）：挂载读（对可见页签白名单校验，非法值回落
 * 首个页签）、切换 router.replace 写回——刷新/分享链接保持当前页签。
 */
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { helpTabs } from '@/help/content'

const auth = useAuthStore()
// useRoute/useRouter 在无 router 上下文（单测直挂）时为 undefined：可选链兜底，
// query 同步只是增强路径（DocumentsView 预填同款口径）
const route = useRoute()
const router = useRouter()

const visibleTabs = computed(() => helpTabs.filter((t) => !t.adminOnly || auth.isAdmin))

/** 挂载读 query.tab（M40）：仅接受当前可见页签名，非法/越权值回落首个页签 */
function initialTab(): string {
  const queryTab = route?.query?.tab
  const name = typeof queryTab === 'string' ? queryTab : ''
  return visibleTabs.value.some((t) => t.name === name) ? name : (visibleTabs.value[0]?.name ?? '')
}

const activeTab = ref<string>(initialTab())

/** 切换页签写回 query（M40）：replace 不留历史记录，其余 query 键原样保留 */
function onTabChange(name: string | number): void {
  void router?.replace({ query: { ...route?.query, tab: String(name) } })
}
</script>

<template>
  <div class="help">
    <el-alert
      type="info"
      show-icon
      :closable="false"
      title="本页是系统内置的快速指引；更详细的试点版手册见管理员发布的《用户手册》文档。"
    />

    <el-tabs v-model="activeTab" class="tabs" @tab-change="onTabChange">
      <el-tab-pane v-for="tab in visibleTabs" :key="tab.name" :label="tab.label" :name="tab.name">
        <section v-for="section in tab.sections" :key="section.title" class="section">
          <h3 class="section-title">{{ section.title }}</h3>
          <p v-if="section.intro" class="section-intro">{{ section.intro }}</p>

          <template v-for="(block, bi) in section.blocks" :key="bi">
            <ol v-if="block.kind === 'steps'" class="steps">
              <li v-for="step in block.steps" :key="step.title">
                <span class="step-title">{{ step.title }}</span>
                <span class="step-detail">{{ step.detail }}</span>
              </li>
            </ol>

            <ul v-else-if="block.kind === 'tips'" class="tips">
              <li v-for="(item, ti) in block.items" :key="ti">{{ item }}</li>
            </ul>

            <div v-else-if="block.kind === 'table'" class="table-wrap">
              <table class="table">
                <thead>
                  <tr>
                    <th v-for="h in block.headers" :key="h">{{ h }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(row, ri) in block.rows" :key="ri">
                    <td v-for="(cell, ci) in row" :key="ci">{{ cell }}</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <el-alert
              v-else-if="block.kind === 'note'"
              :type="block.type"
              show-icon
              :closable="false"
              class="note"
            >
              <!-- 长提示走 description（default slot），粗体 title 仅适合短句 -->
              <span>{{ block.text }}</span>
            </el-alert>
          </template>
        </section>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.help {
  max-width: 960px;
  margin: 0 auto;
}
.tabs {
  margin-top: 16px;
}
.section {
  padding: 8px 0 20px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.section:last-child {
  border-bottom: none;
}
.section-title {
  margin: 0 0 8px;
  font-size: 15px;
  font-weight: 600;
}
.section-intro {
  margin: 0 0 8px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.steps {
  margin: 0;
  padding-left: 20px;
}
.steps li {
  margin-bottom: 8px;
  line-height: 1.7;
}
.step-title {
  font-weight: 600;
  margin-right: 8px;
}
.step-detail {
  color: var(--el-text-color-regular);
}
.tips {
  margin: 0;
  padding-left: 20px;
}
.tips li {
  margin-bottom: 6px;
  line-height: 1.7;
}
.table-wrap {
  overflow-x: auto;
}
.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.table th,
.table td {
  border: 1px solid var(--el-border-color-lighter);
  padding: 8px 10px;
  text-align: left;
  line-height: 1.6;
  vertical-align: top;
}
.table th {
  background: var(--el-fill-color-light);
  font-weight: 600;
  white-space: nowrap;
}
.note {
  margin-top: 4px;
}
</style>
