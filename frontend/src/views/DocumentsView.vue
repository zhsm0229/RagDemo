<script setup lang="ts">
/**
 * P3 知识库·文档管理（ui-spec §三，RAGFlow 模式 §8.2；对照原型 DocView）：
 * 筛选行（部门/状态/关键字 + 查询/重置同行 §22.1）+ 工具栏（上传/zip 导入/草稿审核/手动刷新）
 * + 表格（标题含版本与降级标注/部门/状态机/责任人/生效日期/操作）+ 分页（10/20/50/100 默认 20 右下）。
 * 状态轮询（§8.4）：列表存在 parsing 行时每 30s 静默自动刷新（不闪表格 loading），
 * 组件卸载清理定时器；增删操作成功 ElMessage、失败由 client 统一 toast，上传 422/409 在弹窗内联（§22.2）。
 * 操作列：预览（在线预览弹窗）/原文件（下载）/删除（软删+二次确认）均已后端可用。
 * 草稿审核（agent-evolution #05）：工具栏「草稿审核」一键筛出待审核草稿；行内通过
 * （confirm 双示审计提示）/驳回（prompt 必填原因）；红线：无审核不落地——驳回永不入库，
 * 通过才进解析管道。草稿不显示"确认激活"（激活权在审核流，非敏感待确认语义）。
 * query 预填（#08 去审核 + M42 零召回/待过期 keyword 落点）：白名单键消费后
 * router.replace 清残留（B10）；空态四分支：加载失败可重试（B11）> 草稿审核 >
 * 筛选无结果（B9）> 真空态；审核操作行级在飞锁（B8）；部门列挂检索可见性提示（B12）。
 */
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  approveDraft,
  confirmDocument,
  listDocuments,
  rejectDraft,
  retryDocument,
  type DocumentItem,
  type DocumentSourceType,
  type DocumentStatus,
  type ReviewStatus,
  docFileUrl,
  deleteDocument,
} from '@/api/documents'
import {
  DEPT_LABELS,
  DEPT_OPTIONS,
  REVIEW_STATUS_TAGS,
  STATUS_OPTIONS,
} from '@/components/documents/options'
import DocPreviewDialog from '@/components/documents/DocPreviewDialog.vue'
import UploadDialog from '@/components/documents/UploadDialog.vue'
import ZipImportDialog from '@/components/documents/ZipImportDialog.vue'

const POLL_INTERVAL_MS = 30_000

/** 部门可见性提示（B12，管理员视角通用简化句）：挂在部门列，提醒检索范围口径 */
const DEPT_VISIBILITY_HINT = 'service/hr 部门专属文档不参与你的问答检索'

const route = useRoute()
const router = useRouter()

const filters = reactive({
  dept: '',
  status: '' as DocumentStatus | '',
  keyword: '',
  sourceType: '' as DocumentSourceType | '',
  reviewStatus: '' as ReviewStatus | '',
})
const items = ref<DocumentItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
/** 加载失败标记（B11）：失败 ≠ 没有数据，空态区呈现"可重试"而非"还没有" */
const loadError = ref(false)
/** 草稿审核行级在飞标记（B8）：通过/驳回请求挂起期间禁用按钮防连点重复提交 */
const actingId = ref<number | null>(null)
const uploadVisible = ref(false)
const zipVisible = ref(false)
const previewVisible = ref(false)
const previewDoc = ref<DocumentItem | null>(null)

/** 草稿审核视图激活态：来源=草稿 且 审核状态=待审核 的快捷筛选（工具栏按钮高亮） */
const isDraftReview = computed(
  () => filters.sourceType === 'draft' && filters.reviewStatus === '待审核',
)

/** 空态双分支判定（B9，照 MemoryView §2.5 范式）：dept/status/keyword 任一激活即"筛选无结果" */
const hasFilter = computed(
  () => filters.dept !== '' || filters.status !== '' || filters.keyword.trim() !== '',
)

let pollTimer: number | null = null

async function load(silent = false): Promise<void> {
  if (!silent) loading.value = true
  try {
    const data = await listDocuments({
      dept: filters.dept,
      status: filters.status,
      keyword: filters.keyword,
      sourceType: filters.sourceType,
      reviewStatus: filters.reviewStatus,
      page: page.value,
      pageSize: pageSize.value,
    })
    items.value = data.items
    total.value = data.total
    if (!silent) loadError.value = false
  } catch {
    // client 已统一 toast；非 silent 拉取失败标错误态（B11）——silent 轮询失败不打断展示
    if (!silent) loadError.value = true
  } finally {
    if (!silent) loading.value = false
  }
  syncPolling()
}

/** 加载失败重试（B11）：保留当前筛选与分页原地重拉 */
function retryLoad(): void {
  void load()
}

/** parsing 行存在才维持 30s 轮询；全部到终态即停。轮询走 silent：后台刷新不闪 loading（M-6） */
function syncPolling(): void {
  const hasParsing = items.value.some((doc) => doc.status === 'parsing')
  if (hasParsing && pollTimer === null) {
    pollTimer = window.setInterval(() => void load(true), POLL_INTERVAL_MS)
  } else if (!hasParsing && pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

/** 弹窗关闭即刷新（§22.3 关闭即刷新语义）：失败提交的行（解析失败/待确认）
 * 同样已落库，无条件刷新保证立即可见（取消路径多一次廉价列表调用，可接受）。 */
function onUploadClosed(): void {
  uploadVisible.value = false
  refresh()
}

function refresh(): void {
  void load()
}

/**
 * 路由 query 预填筛选（agent-evolution #08 模板生成"去审核"落点 + M42 零召回/待过期
 * 行跳转 keyword 落点）：参数名对齐本页筛选字段（sourceType/reviewStatus/keyword），
 * 白名单外的值忽略，非法参数不报错只回落全量列表。预填只做一次：消费后立即
 * router.replace 清掉已消费键（B10），用户改条件刷新不再被 query 盖回。
 */
function prefillFromQuery(): void {
  // useRoute 在无 router 上下文（单测直挂）时为 undefined：可选链兜底，预填只是增强路径
  const query = route?.query
  const sourceType = query?.sourceType
  const reviewStatus = query?.reviewStatus
  if (sourceType === 'upload' || sourceType === 'draft') filters.sourceType = sourceType
  if (reviewStatus === '待审核' || reviewStatus === '已通过' || reviewStatus === '已驳回') {
    filters.reviewStatus = reviewStatus
  }
  const keyword = query?.keyword
  if (typeof keyword === 'string' && keyword !== '') filters.keyword = keyword
}

/** 清掉已消费的预填键（B10）：其余 query 键原样保留 */
function stripConsumedQuery(): void {
  if (route?.query === undefined || router === undefined) return
  const rest = { ...route.query }
  delete rest.sourceType
  delete rest.reviewStatus
  delete rest.keyword
  void router.replace({ query: rest })
}

onMounted(() => {
  prefillFromQuery()
  stripConsumedQuery()
  void load()
})
onUnmounted(() => {
  if (pollTimer !== null) window.clearInterval(pollTimer)
  pollTimer = null
})

function search(): void {
  page.value = 1
  void load()
}

/** 草稿审核入口：一键切换"draft + 待审核"快捷筛选（再点还原全量列表） */
function toggleDraftReview(): void {
  if (isDraftReview.value) {
    filters.sourceType = ''
    filters.reviewStatus = ''
  } else {
    filters.sourceType = 'draft'
    filters.reviewStatus = '待审核'
  }
  search()
}

function resetFilters(): void {
  filters.dept = ''
  filters.status = ''
  filters.keyword = ''
  filters.sourceType = ''
  filters.reviewStatus = ''
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

function deptName(code: string): string {
  return DEPT_LABELS[code] ?? code
}

/** 审核状态 → 语义色（模板 row 为 any，索引 REVIEW_STATUS_TAGS 会触发 TS7053，收敛到函数） */
function reviewTagType(status: ReviewStatus): 'success' | 'warning' | 'danger' | 'info' {
  return REVIEW_STATUS_TAGS[status].type
}

async function retry(row: DocumentItem): Promise<void> {
  await retryDocument(row.id)
  ElMessage.success('重试已提交，文档重新进入解析队列')
  void load()
}

function openPreview(row: DocumentItem): void {
  previewDoc.value = row
  previewVisible.value = true
}

async function removeDoc(row: DocumentItem): Promise<void> {
  const action = await ElMessageBox.confirm(
    `删除后文档不可恢复（软删保留审计），检索与问答将不再命中「${row.title}」。确认删除？`,
    '删除文档',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
  ).catch(() => 'cancel')
  if (action === 'cancel') return
  await deleteDocument(row.id)
  ElMessage.success('文档已删除')
  void load()
}

async function confirmRow(row: DocumentItem): Promise<void> {
  await confirmDocument(row.id)
  ElMessage.success('文档已确认激活，参与检索')
  void load()
}

/** 草稿审核通过（agent-evolution #05）：confirm 双示审计提示；通过后进解析入库管道。
 * 行级在飞锁（B8）：入口即占 actingId，确认框挂起/请求在飞期间吞掉重复与并发点击。 */
async function approveDraftRow(row: DocumentItem): Promise<void> {
  if (actingId.value !== null) return
  actingId.value = row.id
  try {
    const action = await ElMessageBox.confirm(
      `通过后「${row.title}」将立即进入解析入库管道，收口后可被检索命中；本操作将记入审计日志。确认通过？`,
      '草稿审核 · 通过',
      { type: 'warning', confirmButtonText: '通过并入库', cancelButtonText: '取消' },
    ).catch(() => 'cancel')
    if (action === 'cancel') return
    await approveDraft(row.id)
    ElMessage.success('草稿已通过，进入解析入库队列')
    void load()
  } finally {
    actingId.value = null
  }
}

/** 草稿驳回：prompt 必填原因（弹窗校验 + 视图内联双闸），原因留档并记入审计日志。
 * 行级在飞锁（B8）：同通过口径。 */
async function rejectDraftRow(row: DocumentItem): Promise<void> {
  if (actingId.value !== null) return
  actingId.value = row.id
  try {
    const result = await ElMessageBox.prompt(
      '驳回后草稿永不入库（不进解析与检索），原因留档并记入审计日志。',
      '草稿审核 · 驳回',
      {
        type: 'warning',
        confirmButtonText: '确认驳回',
        cancelButtonText: '取消',
        inputPlaceholder: '请填写驳回原因（必填）',
        inputValidator: (input: string) => (input.trim() === '' ? '驳回原因不能为空' : true),
      },
    ).catch(() => null)
    if (result === null) return
    const reason = String(result.value ?? '').trim()
    if (reason === '') {
      ElMessage.warning('请填写驳回原因后再提交')
      return
    }
    await rejectDraft(row.id, reason)
    ElMessage.success('草稿已驳回')
    void load()
  } finally {
    actingId.value = null
  }
}

function onUploaded(): void {
  uploadVisible.value = false
  page.value = 1
  void load()
}

function onZipImported(): void {
  void load()
}
</script>

<template>
  <div>
    <div class="filter-row">
      <el-select
        v-model="filters.dept"
        placeholder="部门"
        clearable
        style="width: 130px"
        @change="search"
      >
        <el-option v-for="o in DEPT_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <el-select
        v-model="filters.status"
        placeholder="状态"
        clearable
        style="width: 130px"
        @change="search"
      >
        <el-option v-for="o in STATUS_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <el-input
        v-model="filters.keyword"
        placeholder="标题关键字"
        style="width: 200px"
        clearable
        @keyup.enter="search"
        @clear="search"
      />
      <el-button type="primary" @click="search">查询</el-button>
      <el-button @click="resetFilters">重置</el-button>
      <span class="spacer"></span>
      <el-button :loading="loading" @click="refresh">刷新</el-button>
      <el-button :type="isDraftReview ? 'primary' : 'default'" @click="toggleDraftReview">
        草稿审核
      </el-button>
      <el-button @click="zipVisible = true">zip 批量导入</el-button>
      <el-button type="primary" @click="uploadVisible = true">＋ 上传文档</el-button>
    </div>

    <el-table v-loading="loading" :data="items" border stripe>
      <el-table-column label="标题" min-width="200">
        <template #default="{ row }">
          {{ row.title }} <span class="ver">v{{ row.version }}</span>
          <el-tag v-if="row.sourceType === 'draft'" size="small" class="gap-tag">草稿</el-tag>
          <el-tag v-if="row.degraded" size="small" type="info" class="gap-tag">降级解析</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="部门" width="90">
        <template #default="{ row }">
          <!-- 部门可见性提示（B12）：管理员视角通用简化句，提醒该列文档的检索可见口径 -->
          <el-tooltip :content="DEPT_VISIBILITY_HINT" placement="top">
            <span class="dept-cell">{{ deptName(row.deptCode) }}</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="220">
        <template #default="{ row }">
          <el-tag v-if="row.status === 'ready'" type="success">● 就绪</el-tag>
          <span v-else-if="row.status === 'parsing'" class="parsing-cell">
            <el-tag type="warning">◐ {{ row.parseStage || '解析中' }}</el-tag>
            <!-- 真实进度（后端按阶段/嵌入批次落库）；无值时回退不确定动画（如旧数据） -->
            <el-progress
              v-if="row.parseProgress != null"
              class="bar"
              :percentage="row.parseProgress"
              :stroke-width="6"
            />
            <el-progress
              v-else
              class="bar"
              :percentage="50"
              :indeterminate="true"
              :show-text="false"
              :stroke-width="6"
              :duration="3"
            />
          </span>
          <el-tooltip
            v-else-if="row.status === 'failed'"
            :content="
              (row.parseError || '解析失败') +
              (row.parseErrorDetail ? '\n' + row.parseErrorDetail : '')
            "
            placement="top"
            popper-class="parse-err-tip"
          >
            <el-tag type="danger">✖ 失败(悬停看原因)</el-tag>
          </el-tooltip>
          <el-tag v-else type="info">◔ 排队中</el-tag>
          <el-tag
            v-if="!row.isActive && row.sourceType !== 'draft'"
            type="warning"
            size="small"
            class="gap-tag"
          >
            待确认
          </el-tag>
          <!-- 草稿审核状态（agent-evolution #05）：已驳回悬停看原因 -->
          <el-tooltip
            v-if="row.reviewStatus === '已驳回' && row.rejectReason"
            :content="row.rejectReason"
            placement="top"
          >
            <el-tag size="small" type="danger" class="gap-tag">已驳回</el-tag>
          </el-tooltip>
          <el-tag
            v-else-if="row.reviewStatus"
            size="small"
            :type="reviewTagType(row.reviewStatus)"
            class="gap-tag"
          >
            {{ row.reviewStatus }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="责任人" width="100">
        <template #default="{ row }">{{ row.ownerName }}</template>
      </el-table-column>
      <el-table-column label="生效日期" width="110">
        <template #default="{ row }">{{ row.effectiveDate }}</template>
      </el-table-column>
      <el-table-column label="操作" width="290" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="row.sourceType === 'draft' && row.reviewStatus === '待审核'"
            link
            type="success"
            :disabled="actingId !== null"
            @click="approveDraftRow(row)"
          >
            通过
          </el-button>
          <el-button
            v-if="row.sourceType === 'draft' && row.reviewStatus === '待审核'"
            link
            type="danger"
            :disabled="actingId !== null"
            @click="rejectDraftRow(row)"
          >
            驳回
          </el-button>
          <el-button v-if="row.status === 'failed'" link type="warning" @click="retry(row)">
            重试
          </el-button>
          <el-button
            v-if="!row.isActive && row.sourceType !== 'draft'"
            link
            type="success"
            @click="confirmRow(row)"
          >
            确认激活
          </el-button>
          <el-button v-if="row.status === 'ready'" link type="primary" @click="openPreview(row)">
            预览
          </el-button>
          <el-button
            v-if="row.status === 'ready'"
            link
            type="primary"
            tag="a"
            :href="docFileUrl(row.id)"
            target="_blank"
            rel="noopener"
          >
            原文件
          </el-button>
          <el-button link type="danger" @click="removeDoc(row)">删除</el-button>
        </template>
      </el-table-column>
      <!-- 空态多分支：加载失败可重试（B11）> 草稿审核专属空态 > 筛选无结果（B9，照
           MemoryView §2.5 范式）> 真空态引导上传 -->
      <template #empty>
        <div
          v-if="loadError"
          class="load-error"
          data-test="documents-load-error"
          role="alert"
          @click="retryLoad"
        >
          加载失败，点击重试
        </div>
        <el-empty v-else-if="isDraftReview" description="暂无待审核草稿" :image-size="60" />
        <el-empty
          v-else-if="hasFilter"
          description="没有匹配筛选条件的文档"
          :image-size="60"
          data-test="documents-empty-filtered"
        >
          <el-button size="small" @click="resetFilters">清除筛选</el-button>
        </el-empty>
        <el-empty v-else description="还没有文档" :image-size="60">
          <el-button type="primary" size="small" @click="uploadVisible = true"
            >上传第一份文档</el-button
          >
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

    <UploadDialog :visible="uploadVisible" @close="onUploadClosed" @uploaded="onUploaded" />
    <ZipImportDialog :visible="zipVisible" @close="zipVisible = false" @imported="onZipImported" />
    <DocPreviewDialog
      :visible="previewVisible"
      :target="previewDoc"
      @close="previewVisible = false"
    />
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
/* disabled 按钮不产生鼠标事件：tooltip 挂在包裹 span 上（Element Plus 推荐姿势） */
.tip-host {
  display: inline-flex;
}
.ver {
  color: var(--el-text-color-secondary);
}
.parsing-cell {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.parsing-cell .bar {
  width: 90px;
}
/* 失败 tooltip 双行（友好原因 + 技术详情），popper 挂 body 需全局样式 */
:global(.parse-err-tip) {
  white-space: pre-line;
  max-width: 460px;
}
.gap-tag {
  margin-left: 4px;
}
/* 部门列 tooltip 宿主（B12）：span 便于 hover 命中 */
.dept-cell {
  cursor: default;
}
/* 加载失败可重试空态（B11）：整块可点，颜色区分于普通空态 */
.load-error {
  padding: 24px 0;
  text-align: center;
  color: var(--el-color-danger);
  font-size: 13px;
  cursor: pointer;
}
</style>
