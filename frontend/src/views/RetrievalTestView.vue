<script setup lang="ts">
/**
 * P5 检索测试（ui-spec §五，RAGFlow Retrieval Testing 模式，admin 只读调参工具 F-503）：
 * 部门范围下拉 + 问题输入 + 测试按钮 → RetrievalResultCard 结果渲染（工单 30 抽取共用）。
 * 工单 30 参数对比模式（缺省关=现状单次视图零变化）：开时 2-3 档参数输入（qa/refuse
 * 阈值、topK/topN/efSearch，空=线上值），**串行**执行（TEI CPU 串行下并发互相排队），
 * 单档失败不阻塞他档、逐档独立错误态（W5 错误态红线）；degraded 档标注对比结论不可信。
 * 422 内联（§22.2）；零召回块清单折叠区照旧（F-1011，独立面板互不依赖）。
 */
import { ref } from 'vue'
import { ApiClientError } from '@/api/client'
import {
  runRetrievalTest,
  type RetrievalTestRequest,
  type RetrievalTestResult,
} from '@/api/retrievalTest'
import RetrievalResultCard from '@/components/admin/RetrievalResultCard.vue'
import ZeroRecallChunkPanel from '@/components/admin/ZeroRecallChunkPanel.vue'
import { checkQuestion } from '@/validators'

/** 部门范围选项：后端 deptCode 枚举 service/hr/all（api.md §4.4.1），默认客服（对照原型） */
const SCOPE_OPTIONS = [
  { value: 'service', label: '客服' },
  { value: 'hr', label: '人力' },
  { value: 'all', label: '全员' },
] as const

/** 档位标签（对比模式）：最多 3 档（design/30 裁决 3） */
const VARIANTS = ['档位 A', '档位 B', '档位 C'] as const

const scope = ref<'service' | 'hr' | 'all'>('service')
const question = ref('')
const questionErr = ref('')
const error = ref('')
const testing = ref(false)
const result = ref<RetrievalTestResult | null>(null)

/** 对比模式状态：开关（缺省关）+ 每档参数（空串=线上值）+ 每档结果/错误/进行态 */
const compareMode = ref(false)
type VariantParams = {
  qaThreshold: string
  refuseThreshold: string
  topK: string
  topN: string
  efSearch: string
}
const emptyVariant = (): VariantParams => ({
  qaThreshold: '',
  refuseThreshold: '',
  topK: '',
  topN: '',
  efSearch: '',
})
const variants = ref<VariantParams[]>([emptyVariant(), emptyVariant()])
type VariantOutcome = { result: RetrievalTestResult | null; error: string; running: boolean }
const outcomes = ref<VariantOutcome[]>([
  { result: null, error: '', running: false },
  { result: null, error: '', running: false },
])
const comparing = ref(false)

/** 单档参数 → 请求体片段（空串/非法值不传=线上值；数值化在此收敛） */
function buildVariantBody(v: VariantParams): Partial<RetrievalTestRequest> {
  const body: Record<string, number> = {}
  const num = (s: string): number | undefined => {
    const t = s.trim()
    if (t === '') return undefined
    const n = Number(t)
    return Number.isFinite(n) ? n : undefined
  }
  for (const [k, raw] of Object.entries({
    qaThreshold: v.qaThreshold,
    refuseThreshold: v.refuseThreshold,
    topK: v.topK,
    topN: v.topN,
    efSearch: v.efSearch,
  })) {
    const n = num(raw)
    if (n !== undefined) body[k] = n
  }
  return body
}

/** 单档执行：结果/错误写回各自槽位，异常不跨档传播（R5 逐档失败态） */
async function runVariant(
  index: number,
  base: { question: string; deptCode: 'service' | 'hr' | 'all' },
): Promise<void> {
  outcomes.value[index]!.running = true
  outcomes.value[index]!.error = ''
  try {
    outcomes.value[index]!.result = await runRetrievalTest({
      ...base,
      ...buildVariantBody(variants.value[index]!),
    })
  } catch (e) {
    outcomes.value[index]!.error = e instanceof ApiClientError ? e.message : '系统繁忙，请稍后重试'
  } finally {
    outcomes.value[index]!.running = false
  }
}

/** 档位增减（模板内联多语句不合法，收敛为 handler；上限 3 档 design/30 裁决 3） */
function addVariant(): void {
  if (variants.value.length < 3) {
    variants.value.push(emptyVariant())
    outcomes.value.push({ result: null, error: '', running: false })
  }
}

function removeVariant(): void {
  if (variants.value.length > 2) {
    variants.value.pop()
    outcomes.value.pop()
  }
}

/** 对比模式入口：校验 → 串行逐档执行（R5：TEI CPU 串行，避免互相排队） */
async function runCompare(): Promise<void> {
  questionErr.value = ''
  const check = checkQuestion(question.value)
  if (check !== true) {
    questionErr.value = check
    return
  }
  comparing.value = true
  try {
    for (let i = 0; i < variants.value.length; i++) {
      await runVariant(i, { question: question.value.trim(), deptCode: scope.value })
    }
  } finally {
    comparing.value = false
  }
}

/** 单次模式（现状回归）：单请求单结果 */
async function run(): Promise<void> {
  questionErr.value = ''
  error.value = ''
  const check = checkQuestion(question.value)
  if (check !== true) {
    questionErr.value = check
    return
  }
  testing.value = true
  try {
    result.value = await runRetrievalTest({
      question: question.value.trim(),
      deptCode: scope.value,
    })
  } catch (e) {
    error.value = e instanceof ApiClientError ? e.message : '系统繁忙，请稍后重试'
  } finally {
    testing.value = false
  }
}
</script>

<template>
  <div class="page">
    <div class="panel">
      <h4>检索测试（admin 调试工具 · F-503）</h4>
      <div class="filter-row">
        <span class="field-label">部门范围</span>
        <el-select v-model="scope" style="width: 140px">
          <el-option v-for="o in SCOPE_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
        </el-select>
        <el-checkbox v-model="compareMode" class="compare-switch">参数对比</el-checkbox>
      </div>
      <div class="filter-row">
        <el-input
          v-model="question"
          placeholder="输入测试问题，例如：退货运费谁承担 / 年假怎么算"
          maxlength="2000"
          style="max-width: 520px"
          @keyup.enter="compareMode ? runCompare() : run()"
        />
        <el-button v-if="!compareMode" type="primary" :loading="testing" @click="run"
          >测 试</el-button
        >
        <el-button v-else type="primary" :loading="comparing" @click="runCompare"
          >对比测试</el-button
        >
      </div>
      <div v-if="questionErr" class="err-inline" role="alert">{{ questionErr }}</div>
      <div v-if="error" class="err-inline" role="alert">{{ error }}</div>

      <!-- 对比模式：逐档参数行 + 串行结果 -->
      <template v-if="compareMode">
        <div v-for="(v, i) in variants" :key="i" class="filter-row variant-row">
          <el-tag size="small" type="info">{{ VARIANTS[i] }}</el-tag>
          <el-input v-model="v.qaThreshold" placeholder="qa 阈值" style="width: 110px" />
          <el-input v-model="v.refuseThreshold" placeholder="拒答阈值" style="width: 110px" />
          <el-input v-model="v.topK" placeholder="topK" style="width: 90px" />
          <el-input v-model="v.topN" placeholder="topN" style="width: 90px" />
          <el-input v-model="v.efSearch" placeholder="efSearch" style="width: 110px" />
          <!-- 原生 button：el-button 经组件 emit 的 click 在 jsdom 不可靠（工单 25 同款处置） -->
          <button
            v-if="i === variants.length - 1 && variants.length < 3"
            type="button"
            class="link-btn"
            @click="addVariant"
          >
            + 加档位
          </button>
          <button
            v-if="variants.length > 2 && i === variants.length - 1"
            type="button"
            class="link-btn"
            @click="removeVariant"
          >
            - 减档位
          </button>
        </div>

        <div v-for="(o, i) in outcomes" :key="`o-${i}`" class="variant-outcome">
          <div v-if="o.running" class="muted">测试中…</div>
          <div v-else-if="o.error" class="err-inline" role="alert">
            {{ VARIANTS[i] }}：{{ o.error }}
          </div>
          <RetrievalResultCard
            v-else-if="o.result"
            :title="`${VARIANTS[i]}（qa ${o.result.thresholds.qa} / 拒答 ${o.result.thresholds.refuse} / topK ${o.result.params.topK} / ef ${o.result.params.efSearch}）`"
            :result="o.result"
          />
        </div>
      </template>

      <!-- 单次模式（现状） -->
      <template v-else-if="result">
        <RetrievalResultCard title="问答对命中" :result="result" />
      </template>
    </div>

    <div class="panel">
      <ZeroRecallChunkPanel />
    </div>
  </div>
</template>

<style scoped>
.page {
  max-width: 900px;
}
.page > .panel + .panel {
  margin-top: 12px;
}
.filter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.compare-switch {
  margin-left: 8px;
}
.variant-row {
  margin-bottom: 8px;
}
.variant-outcome {
  margin-top: 12px;
}
.link-btn {
  background: none;
  border: none;
  color: var(--el-color-primary);
  cursor: pointer;
  font-size: 12px;
  padding: 0 4px;
}
.muted {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.err-inline {
  color: var(--el-color-danger);
  font-size: 12px;
  margin-top: 4px;
}
</style>
