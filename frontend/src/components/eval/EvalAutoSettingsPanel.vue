<script setup lang="ts">
/**
 * 自动评测设置面板（评测集自动更新 09 票，api.md §4.2.14/§4.2.15）：
 * 总开关 / 合成预算（每种子变体数、每周抽样块数、每周总题量上限）/ 退化阈值，
 * PUT 全量覆写持久化在 app_settings（重启不丢）。周任务时刻固定（周日 04:00 合成
 * 流水、05:00 三视角跑分，UTC），页面只读说明不提供 cron 编辑（改时刻走代码评审）。
 */
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ApiClientError } from '@/api/client'
import { getEvalAutoConfig, putEvalAutoConfig } from '@/api/evalAdmin'

const enabled = ref(false)
const synthPerSeed = ref(2)
const docSamplePerDept = ref(20)
const weeklyMaxQuestions = ref(100)
const regressionPp = ref(5.0)
const loading = ref(false)
const saving = ref(false)
/** 配置加载失败标记（工单 08 / P1-10）：表单停在默认值时保存=把线上配置覆写回默认，必须禁保存 */
const loadErr = ref('')

async function load(): Promise<void> {
  loading.value = true
  try {
    const config = await getEvalAutoConfig()
    enabled.value = config.enabled
    synthPerSeed.value = config.synthPerSeed
    docSamplePerDept.value = config.docSamplePerDept
    weeklyMaxQuestions.value = config.weeklyMaxQuestions
    regressionPp.value = config.regressionPp
    loadErr.value = ''
  } catch (e) {
    loadErr.value = e instanceof ApiClientError ? e.message : '配置加载失败，请刷新重试'
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  saving.value = true
  try {
    await putEvalAutoConfig({
      enabled: enabled.value,
      synthPerSeed: synthPerSeed.value,
      docSamplePerDept: docSamplePerDept.value,
      weeklyMaxQuestions: weeklyMaxQuestions.value,
      regressionPp: regressionPp.value,
    })
    ElMessage.success('评测自动配置已保存（周任务按新配置执行）')
  } finally {
    saving.value = false
  }
}

onMounted(() => void load())
</script>

<template>
  <div v-loading="loading" class="settings">
    <!-- 加载失败禁保存（P1-10）：错误说明置顶，保存按钮随 loadErr 禁用 -->
    <el-alert
      v-if="loadErr"
      type="error"
      title="配置加载失败，为避免覆写线上配置已禁用保存，请刷新重试"
      :closable="false"
      show-icon
      class="load-err"
      data-test="auto-config-load-err"
    />
    <el-form label-width="180px" style="max-width: 640px">
      <el-form-item label="自动闭环总开关">
        <el-switch v-model="enabled" />
        <span class="hint">关 = 周任务零副作用；手动触发的合成不受此开关限制</span>
      </el-form-item>
      <el-form-item label="每种子变体题数">
        <el-input-number v-model="synthPerSeed" :min="1" :max="50" />
        <span class="hint">每个点踩难例种子最多合成的变体题数（LLM 预算）</span>
      </el-form-item>
      <el-form-item label="每周抽样知识块/部门">
        <el-input-number v-model="docSamplePerDept" :min="1" :max="500" />
        <span class="hint">文档造题通道每部门每周抽样块数（零召回块优先）</span>
      </el-form-item>
      <el-form-item label="每周新增题量上限">
        <el-input-number v-model="weeklyMaxQuestions" :min="1" :max="5000" />
        <span class="hint">两通道共享的每周自动新增评测题总量上限</span>
      </el-form-item>
      <el-form-item label="退化告警阈值（pp）">
        <el-input-number v-model="regressionPp" :min="0.5" :max="50" :step="0.5" />
        <span class="hint">相邻两轮准确率降幅超过该百分点数即标红（健康卡评测质量项）</span>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="saving" :disabled="loadErr !== ''" @click="save">
          保存配置
        </el-button>
      </el-form-item>
    </el-form>
    <el-alert type="info" :closable="false" class="schedule-note">
      <p>
        定时计划（UTC）：周日 04:00 合成流水（采集 → 难例合成 → 文档造题）；周日 05:00
        三视角（客服/人力/全员）自动跑分。
      </p>
      <p>自动题目全部落「扩充集」，不进发版门禁口径；升格在「评测题库」页签人工完成。</p>
      <p>「生成变体题」等手动按钮不受本页预算约束。</p>
    </el-alert>
  </div>
</template>

<style scoped>
.settings {
  max-width: 720px;
}
.hint {
  margin-left: 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.load-err {
  margin-bottom: 12px;
}
.schedule-note {
  margin-top: 8px;
}
.schedule-note p {
  margin: 2px 0;
}
</style>
