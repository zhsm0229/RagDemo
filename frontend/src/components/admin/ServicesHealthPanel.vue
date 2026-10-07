<script setup lang="ts">
/**
 * 服务健康卡（2026-10-02 试点反馈新增）：问答链路依赖探活只读展示。
 * 背景：app 容器 healthy 但 TEI/LLM 挂掉时问答全挂，管理页此前完全不可见。
 * overall 三态：up=问答链路全绿；degraded=仅文档解析不可用（问答不受影响）；
 * down=问答链路任一不可用。异常行给 runbook 修复指引（命令口径同 deploy/recover.sh）。
 * **只读**：不做启停按钮（web 触达容器编排=攻击面质变，2026-10-02 评估结论）。
 * 手动刷新触发探活（探活最坏 2.5s×并发=秒级，不做自动轮询避免常驻压力）。
 */
import { onMounted, ref } from 'vue'
import { ApiClientError } from '@/api/client'
import { getServicesHealth, type ServicesHealthData } from '@/api/overview'

const data = ref<ServicesHealthData | null>(null)
const loadErr = ref('')
const loading = ref(false)

/** 各依赖异常时的修复指引（命令与 deploy/recover.sh / compose 同口径） */
const RUNBOOK: Record<string, string> = {
  pg: 'docker compose up -d pg（启动失败看容器日志）',
  embed: 'bash deploy/recover.sh embed',
  rerank: 'bash deploy/recover.sh rerank',
  llm: '检查 .env 的 LLM_BASE_URL / LLM_API_KEY 配置与外网连通',
  mineru: 'bash deploy/recover.sh mineru（仅影响文档解析，问答不受影响）',
  gotenberg: 'docker compose up -d gotenberg（仅影响 doc/ppt/xls 老格式上传，问答不受影响）',
}

async function load(): Promise<void> {
  loading.value = true
  loadErr.value = ''
  try {
    data.value = await getServicesHealth()
  } catch (e) {
    loadErr.value = e instanceof ApiClientError ? e.message : '健康探活失败，请稍后重试'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="panel" data-test="services-health-panel">
    <div class="head">
      <h4>服务健康</h4>
      <el-button size="small" :loading="loading" data-test="svc-refresh" @click="load"
        >刷新</el-button
      >
    </div>

    <el-alert
      v-if="loadErr"
      type="error"
      :title="loadErr"
      :closable="false"
      show-icon
      data-test="svc-load-err"
    />
    <template v-else-if="data">
      <el-alert
        :type="
          data.overall === 'up' ? 'success' : data.overall === 'degraded' ? 'warning' : 'error'
        "
        :closable="false"
        show-icon
        class="overall"
        data-test="svc-overall"
        :title="
          data.overall === 'up'
            ? '问答链路正常'
            : data.overall === 'degraded'
              ? '问答链路正常；文档解析不可用（上传解析会失败）'
              : '问答链路异常，回答将失败——按下方指引恢复'
        "
      />
      <el-table :data="data.services" size="small" data-test="svc-table">
        <el-table-column prop="label" label="组件" min-width="130" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.status === 'up' ? 'success' : row.status === 'down' ? 'danger' : 'info'"
            >
              {{ row.status === 'up' ? '正常' : row.status === 'down' ? '不可用' : '未启用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="延迟" width="90">
          <template #default="{ row }">
            <span>{{ row.latencyMs ? `${row.latencyMs}ms` : '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="说明 / 修复指引" min-width="260">
          <template #default="{ row }">
            <span v-if="row.status === 'up' && row.detail" class="muted">{{ row.detail }}</span>
            <span v-else-if="row.status === 'up'" class="muted">—</span>
            <template v-else>
              <div class="detail">{{ row.detail }}</div>
              <div class="runbook">{{ RUNBOOK[row.key] ?? '' }}</div>
            </template>
          </template>
        </el-table-column>
      </el-table>
    </template>
  </section>
</template>

<style scoped>
.panel {
  margin-top: 16px;
}
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.head h4 {
  margin: 0;
}
.overall {
  margin-bottom: 8px;
}
.muted {
  color: var(--el-text-color-secondary);
}
.detail {
  line-height: 1.5;
}
.runbook {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  font-family: var(--el-font-family-mono, monospace);
  line-height: 1.5;
}
</style>
