<script setup lang="ts">
/**
 * P10 运营概览（ui-spec §八，对照原型 OverviewView，F-704/F-1009/F-1010）：
 * 统计卡四张（提问/命中率/拒答率/点踩率，el-statistic——deviations #3 正式实现
 * 组件库约束）+ days 切换（近1天/近7天，api.md §4.5.1 仅两档）+ byDept 表格
 * （原型 token 条形图改表格 tokens 列，不引图表依赖，deviations #46）+ 知识缺口
 * TOP 卡片（F-1010，原型缺口卡无端点顺延至 W5 补齐，组件 KnowledgeGapCard）
 * + 健康度面板（就绪率进度条/待确认/零召回榜/待过期清单，行点击跳 P3 文档管理）。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import KnowledgeGapCard from '@/components/overview/KnowledgeGapCard.vue'
import { getHealth, getOverview, type HealthData, type OverviewData } from '@/api/overview'
import { deptCodeLabel } from '@/components/admin/options'
import { fmtTime } from '@/utils/format'

const days = ref<1 | 7>(1)
const loading = ref(false)
const overview = ref<OverviewData | null>(null)
const health = ref<HealthData | null>(null)

const router = useRouter()

async function load(): Promise<void> {
  loading.value = true
  try {
    overview.value = await getOverview(days.value)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void load()
  void getHealth().then((data) => {
    health.value = data
  })
})

function switchDays(value: 1 | 7): void {
  if (value === days.value) return
  days.value = value
  void load()
}

/** 比率 0-1 → 百分比 1 位小数（el-statistic 数值口径，需 number） */
function pct(value: number): number {
  return Number((value * 100).toFixed(1))
}

/** 比率 0-1 → 百分比文本（表格单元格，保留 1 位小数） */
function pctText(value: number): string {
  return `${(value * 100).toFixed(1)}%`
}

const questionsLabel = computed(() => (days.value === 1 ? '今日提问' : '近7天提问'))

function gotoDocuments(): void {
  void router.push({ name: 'kb-documents' })
}

/** 待过期日期标红（含已到期未下线：≤ 今天即告警） */
function expiryClass(expiryDate: string): string {
  return expiryDate <= new Date().toISOString().slice(0, 10) ? 'expired' : ''
}
</script>

<template>
  <div v-loading="loading">
    <div class="days-row">
      <el-radio-group :model-value="days" @update:model-value="switchDays($event as 1 | 7)">
        <el-radio-button :value="1">近1天</el-radio-button>
        <el-radio-button :value="7">近7天</el-radio-button>
      </el-radio-group>
    </div>

    <div v-if="overview" class="stat-cards">
      <div class="stat-card">
        <el-statistic :title="questionsLabel" :value="overview.totals.questions" />
      </div>
      <div class="stat-card">
        <el-statistic
          title="命中率（非拒答）"
          :value="pct(overview.totals.hitRate)"
          :precision="1"
          suffix="%"
        />
      </div>
      <div class="stat-card">
        <el-statistic
          title="拒答率"
          :value="pct(overview.totals.refuseRate)"
          :precision="1"
          suffix="%"
        />
      </div>
      <div class="stat-card">
        <el-statistic
          title="点踩率"
          :value="pct(overview.totals.downRate)"
          :precision="1"
          suffix="%"
          :value-style="{
            color: overview.totals.downRate > 0 ? 'var(--el-color-danger)' : undefined,
          }"
        />
      </div>
    </div>

    <section class="panel" data-test="by-dept-panel">
      <h4>按部门（{{ days === 1 ? '近1天' : '近7天' }}）</h4>
      <el-table v-if="overview" :data="overview.byDept" border size="small">
        <el-table-column label="部门" width="110">
          <template #default="{ row }">{{ deptCodeLabel(row.deptCode) }}</template>
        </el-table-column>
        <el-table-column prop="questions" label="提问数" width="90" />
        <el-table-column label="命中率" width="100">
          <template #default="{ row }">{{ pctText(row.hitRate) }}</template>
        </el-table-column>
        <el-table-column label="拒答率" width="100">
          <template #default="{ row }">{{ pctText(row.refuseRate) }}</template>
        </el-table-column>
        <el-table-column label="点踩率" width="100">
          <template #default="{ row }">{{ pctText(row.downRate) }}</template>
        </el-table-column>
        <el-table-column label="token 消耗" min-width="110">
          <template #default="{ row }">{{ row.tokens.toLocaleString() }}</template>
        </el-table-column>
      </el-table>
    </section>

    <KnowledgeGapCard />

    <section v-if="health" class="panel" data-test="health-panel">
      <h4>语料健康度</h4>
      <div class="health-row">
        <span>文档就绪率（{{ health.readyCount }}/{{ health.totalDocs }}）</span>
        <el-progress class="ready-bar" :percentage="Number((health.readyRate * 100).toFixed(1))" />
      </div>
      <p class="health-line">
        待确认文档：<b>{{ health.pendingConfirm }}</b> 篇
        <el-button link type="primary" @click="gotoDocuments">去处理</el-button>
      </p>
      <h5>零召回文档榜（近 30 天 0 命中，点击查看）</h5>
      <el-table
        :data="health.zeroRecall"
        size="small"
        class="click-table"
        @row-click="gotoDocuments"
      >
        <el-table-column prop="title" label="标题" min-width="200" show-overflow-tooltip />
        <el-table-column label="部门" width="90">
          <template #default="{ row }">{{ deptCodeLabel(row.deptCode) }}</template>
        </el-table-column>
        <el-table-column label="建档时间" width="150">
          <template #default="{ row }">{{ fmtTime(row.createdAt) }}</template>
        </el-table-column>
        <template #empty>暂无零召回文档</template>
      </el-table>
      <h5>待过期清单（7 天内到期，含已到期未下线）</h5>
      <el-table
        :data="health.expiringSoon"
        size="small"
        class="click-table"
        @row-click="gotoDocuments"
      >
        <el-table-column prop="title" label="标题" min-width="200" show-overflow-tooltip />
        <el-table-column label="到期日" width="120">
          <template #default="{ row }">
            <span :class="expiryClass(row.expiryDate)">{{ row.expiryDate }}</span>
          </template>
        </el-table-column>
        <template #empty>暂无待过期文档</template>
      </el-table>
    </section>
  </div>
</template>

<style scoped>
/* 对照原型 .stat-cards/.stat-card 布局（卡面用 el-statistic，deviations #3） */
.stat-cards {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}
.stat-card {
  flex: 1;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 16px;
}
.days-row {
  margin-bottom: 12px;
}
.panel {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 14px;
  margin-bottom: 16px;
}
.panel h4,
.panel h5 {
  margin: 0 0 10px;
  font-size: 14px;
}
.panel h5 {
  margin-top: 12px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.health-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
  font-size: 13px;
}
.ready-bar {
  width: 280px;
}
.health-line {
  margin: 0 0 6px;
  font-size: 13px;
}
.click-table {
  cursor: pointer;
}
.expired {
  color: var(--el-color-danger);
  font-weight: 600;
}
</style>
