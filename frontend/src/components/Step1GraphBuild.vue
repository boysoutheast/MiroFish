<template>
  <div class="workbench-panel">
    <div class="scroll-container">
      <!-- Step 01: Ontology -->
      <div class="step-card" :class="{ 'active': currentPhase === 0, 'completed': currentPhase > 0 }">
        <div class="card-header">
          <div class="step-info">
            <span class="step-num">01</span>
            <span class="step-title">{{ $t('step1.ontologyGeneration') }}</span>
          </div>
          <div class="step-status">
            <span v-if="currentPhase > 0" class="badge success">{{ $t('step1.ontologyCompleted') }}</span>
            <span v-else-if="currentPhase === 0" class="badge processing">{{ $t('step1.ontologyGenerating') }}</span>
            <span v-else class="badge pending">{{ $t('step1.ontologyPending') }}</span>
          </div>
        </div>
        
        <div class="card-content">
          <p class="api-note">POST /api/graph/ontology/generate</p>
          <p class="description">
            {{ $t('step1.ontologyDesc') }}
          </p>

          <!-- Loading / Progress -->
          <div v-if="currentPhase === 0 && ontologyProgress" class="progress-section">
            <div class="spinner-sm"></div>
            <span>{{ ontologyProgress.message || $t('step1.analyzingDocs') }}</span>
          </div>

          <!-- Detail Overlay -->
          <div v-if="selectedOntologyItem" class="ontology-detail-overlay">
            <div class="detail-header">
               <div class="detail-title-group">
                  <span class="detail-type-badge">{{ selectedOntologyItem.itemType === 'entity' ? 'ENTITY' : 'RELATION' }}</span>
                  <span class="detail-name">{{ selectedOntologyItem.name }}</span>
               </div>
               <button class="close-btn" @click="selectedOntologyItem = null">×</button>
            </div>
            <div class="detail-body">
               <div class="detail-desc">{{ selectedOntologyItem.description }}</div>
               
               <!-- Attributes -->
               <div class="detail-section" v-if="selectedOntologyItem.attributes?.length">
                  <span class="section-label">ATTRIBUTES</span>
                  <div class="attr-list">
                     <div v-for="attr in selectedOntologyItem.attributes" :key="attr.name" class="attr-item">
                        <span class="attr-name">{{ attr.name }}</span>
                        <span class="attr-type">({{ attr.type }})</span>
                        <span class="attr-desc">{{ attr.description }}</span>
                     </div>
                  </div>
               </div>

               <!-- Examples (Entity) -->
               <div class="detail-section" v-if="selectedOntologyItem.examples?.length">
                  <span class="section-label">EXAMPLES</span>
                  <div class="example-list">
                     <span v-for="ex in selectedOntologyItem.examples" :key="ex" class="example-tag">{{ ex }}</span>
                  </div>
               </div>

               <!-- Source/Target (Relation) -->
               <div class="detail-section" v-if="selectedOntologyItem.source_targets?.length">
                  <span class="section-label">CONNECTIONS</span>
                  <div class="conn-list">
                     <div v-for="(conn, idx) in selectedOntologyItem.source_targets" :key="idx" class="conn-item">
                        <span class="conn-node">{{ conn.source }}</span>
                        <span class="conn-arrow">→</span>
                        <span class="conn-node">{{ conn.target }}</span>
                     </div>
                  </div>
               </div>
            </div>
          </div>

          <!-- Generated Entity Tags -->
          <div v-if="projectData?.ontology?.entity_types" class="tags-container" :class="{ 'dimmed': selectedOntologyItem }">
            <span class="tag-label">GENERATED ENTITY TYPES</span>
            <div class="tags-list">
              <span 
                v-for="entity in projectData.ontology.entity_types" 
                :key="entity.name" 
                class="entity-tag clickable"
                @click="selectOntologyItem(entity, 'entity')"
              >
                {{ entity.name }}
              </span>
            </div>
          </div>

          <!-- Generated Relation Tags -->
          <div v-if="projectData?.ontology?.edge_types" class="tags-container" :class="{ 'dimmed': selectedOntologyItem }">
            <span class="tag-label">GENERATED RELATION TYPES</span>
            <div class="tags-list">
              <span 
                v-for="rel in projectData.ontology.edge_types" 
                :key="rel.name" 
                class="entity-tag clickable"
                @click="selectOntologyItem(rel, 'relation')"
              >
                {{ rel.name }}
              </span>
            </div>
          </div>
        </div>
      </div>

      <!-- Step 02: Graph Build -->
      <div class="step-card" :class="{ 'active': currentPhase === 1, 'completed': currentPhase > 1 }">
        <div class="card-header">
          <div class="step-info">
            <span class="step-num">02</span>
            <span class="step-title">{{ $t('step1.graphRagBuild') }}</span>
          </div>
          <div class="step-status">
            <span v-if="currentPhase > 1" class="badge success">{{ $t('step1.ontologyCompleted') }}</span>
            <span v-else-if="currentPhase === 1" class="badge processing">{{ buildProgress?.progress || 0 }}%</span>
            <span v-else class="badge pending">{{ $t('step1.ontologyPending') }}</span>
          </div>
        </div>

        <div class="card-content">
          <p class="api-note">POST /api/graph/build</p>
          <p class="description">
            {{ $t('step1.graphRagDesc') }}
          </p>
          
          <!-- Stats Cards -->
          <div class="stats-grid">
            <div class="stat-card">
              <span class="stat-value">{{ graphStats.nodes }}</span>
              <span class="stat-label">{{ $t('step1.entityNodes') }}</span>
            </div>
            <div class="stat-card">
              <span class="stat-value">{{ graphStats.edges }}</span>
              <span class="stat-label">{{ $t('step1.relationEdges') }}</span>
            </div>
            <div class="stat-card">
              <span class="stat-value">{{ graphStats.types }}</span>
              <span class="stat-label">{{ $t('step1.schemaTypes') }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Step 03: Complete -->
      <div class="step-card" :class="{ 'active': currentPhase === 2, 'completed': currentPhase >= 2 }">
        <div class="card-header">
          <div class="step-info">
            <span class="step-num">03</span>
            <span class="step-title">{{ $t('step1.buildComplete') }}</span>
          </div>
          <div class="step-status">
            <span v-if="currentPhase >= 2" class="badge accent">{{ $t('step1.inProgress') }}</span>
          </div>
        </div>
        
        <div class="card-content">
          <p class="api-note">POST /api/simulation/create</p>
          <p class="description">{{ $t('step1.buildCompleteDesc') }}</p>
          <button
            class="action-btn"
            :disabled="currentPhase < 2 || creatingSimulation || checkingExisting"
            @click="handleEnterEnvSetup"
          >
            <span v-if="creatingSimulation || checkingExisting" class="spinner-sm"></span>
            {{ (creatingSimulation && $t('step1.creating')) || (checkingExisting && $t('step1.checkingExisting')) || ($t('step1.enterEnvSetup') + ' ➝') }}
          </button>

          <!-- 检查已有模拟失败 -->
          <div v-if="checkExistingError" class="check-error-box">
            <span class="check-error-text">{{ $t('step1.checkExistingFailed', { error: checkExistingError }) }}</span>
            <button class="retry-btn" :disabled="checkingExisting" @click="handleEnterEnvSetup">{{ $t('step1.retry') }}</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 已有模拟选择弹窗 -->
    <Teleport to="body">
      <div v-if="showSimulationPicker" class="picker-overlay" @click.self="showSimulationPicker = false">
        <div class="picker-content">
          <div class="picker-header">
            <span class="picker-title">{{ $t('step1.selectSimulationTitle') }}</span>
            <button class="close-btn" @click="showSimulationPicker = false">×</button>
          </div>
          <p class="picker-desc">{{ $t('step1.selectSimulationDesc') }}</p>
          <div class="picker-list">
            <div
              v-for="sim in existingSimulations"
              :key="sim.simulation_id"
              class="picker-item"
              @click="selectExistingSimulation(sim)"
            >
              <span class="picker-item-id">{{ sim.simulation_id }}</span>
              <span class="picker-item-date">{{ formatPickerDate(sim.created_at) }}</span>
              <span class="picker-item-status" :class="getSimulationProgress(sim)">
                {{ formatPickerStatus(sim) }}
              </span>
            </div>
          </div>
          <button class="create-new-btn" @click="createNewSimulation">
            {{ $t('step1.createNewSimulation') }}
          </button>
        </div>
      </div>
    </Teleport>

    <!-- Bottom Info / Logs -->
    <div class="system-logs">
      <div class="log-header">
        <span class="log-title">SYSTEM DASHBOARD</span>
        <span class="log-id">{{ projectData?.project_id || 'NO_PROJECT' }}</span>
      </div>
      <div class="log-content" ref="logContent">
        <div class="log-line" v-for="(log, idx) in systemLogs" :key="idx">
          <span class="log-time">{{ log.time }}</span>
          <span class="log-msg">{{ log.msg }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { createSimulation, getSimulationHistory } from '../api/simulation'
import { getSimulationProgress, resolveSimulationRoute } from '../utils/simulationProgress'

const router = useRouter()
const { t } = useI18n()

const props = defineProps({
  currentPhase: { type: Number, default: 0 },
  projectData: Object,
  ontologyProgress: Object,
  buildProgress: Object,
  graphData: Object,
  systemLogs: { type: Array, default: () => [] }
})

defineEmits(['next-step'])

const selectedOntologyItem = ref(null)
const logContent = ref(null)
const creatingSimulation = ref(false)
const checkingExisting = ref(false)
const checkExistingError = ref(null)
const showSimulationPicker = ref(false)
const existingSimulations = ref([])

// 拉取该项目已有的 simulation 列表（纯数据获取，不碰任何 UI 状态）
// 被手动点击（handleEnterEnvSetup）和自动检查（autoCheckExistingSimulation）共用
const fetchMatchingSimulations = async () => {
  const historyRes = await getSimulationHistory(100)
  if (!historyRes.success) {
    throw new Error(historyRes.error || t('common.unknownError'))
  }
  const all = historyRes.data || []
  const missingProjectIdCount = all.filter(sim => !sim.project_id).length
  if (missingProjectIdCount > 0) {
    console.warn(`[Step1GraphBuild] ${missingProjectIdCount} 条模拟记录缺少 project_id，已被过滤，不计入匹配数（可能是后端数据缺陷，不代表真的 0 个已有模拟）`)
  }
  return all.filter(sim => sim.project_id === props.projectData.project_id)
}

// 唯一一个且已有报告 -> 直接跳转 Report，零点击
// 共用于手动点击和自动检查这两条路径
const redirectIfSingleReport = (matches) => {
  if (matches.length === 1 && matches[0].report_id) {
    router.push({
      name: 'Report',
      params: { reportId: matches[0].report_id }
    })
    return true
  }
  return false
}

// 进入环境搭建 - 先检查该项目是否已有 simulation，避免每次都从零新建（Opsi 3）
const handleEnterEnvSetup = async () => {
  if (!props.projectData?.project_id || !props.projectData?.graph_id) {
    console.error('缺少项目或图谱信息')
    return
  }

  checkingExisting.value = true
  checkExistingError.value = null

  let matches = []
  try {
    matches = await fetchMatchingSimulations()
  } catch (err) {
    console.error('检查已有模拟失败:', err)
    checkExistingError.value = err.message || t('common.unknownError')
    checkingExisting.value = false
    return
  }

  checkingExisting.value = false

  if (matches.length === 0) {
    // 没有已有 simulation，行为跟以前一样：直接建新的
    await createNewSimulation()
    return
  }

  if (redirectIfSingleReport(matches)) {
    return
  }

  // 其余情况（1个但没报告，或者 >1个）：给用户选
  existingSimulations.value = matches
  showSimulationPicker.value = true
}

// 组件挂载时自动检查已有 simulation，不用户等点击 "Enter Env Setup" 才发现
// 跟手动点击不同：0 个已有 simulation 时啥都不做（不擅自新建），失败时静默 warn（不打断 Step1 显示）
// 同样要设 checkingExisting，避免自动检查还在飞的时候用户手动点按钮触发第二个并发请求
const autoCheckExistingSimulation = async () => {
  if (!props.projectData?.project_id || !props.projectData?.graph_id) {
    return
  }

  checkingExisting.value = true

  let matches = []
  try {
    matches = await fetchMatchingSimulations()
  } catch (err) {
    console.warn('[Step1GraphBuild] 自动检查已有模拟失败，保持 Step1 正常显示，用户仍可用按钮手动重试:', err)
    checkingExisting.value = false
    return
  }

  checkingExisting.value = false

  if (matches.length === 0) {
    // 0 个已有 simulation -> 什么都不做，留在 Step1，等用户手动点击决定
    return
  }

  if (redirectIfSingleReport(matches)) {
    return
  }

  // 其余情况（1个但没报告，或者 >1个）：自动弹出选择框，用户仍可手动关掉
  existingSimulations.value = matches
  showSimulationPicker.value = true
}

// 建新的 simulation（旧行为，"Buat Baru" 按钮或 0 已有 simulation 时走这里）
const createNewSimulation = async () => {
  showSimulationPicker.value = false
  creatingSimulation.value = true

  try {
    const res = await createSimulation({
      project_id: props.projectData.project_id,
      graph_id: props.projectData.graph_id,
      enable_twitter: true,
      enable_reddit: true
    })

    if (res.success && res.data?.simulation_id) {
      // 跳转到 simulation 页面
      router.push({
        name: 'Simulation',
        params: { simulationId: res.data.simulation_id }
      })
    } else {
      console.error('创建模拟失败:', res.error)
      alert(t('step1.createSimulationFailed', { error: res.error || t('common.unknownError') }))
    }
  } catch (err) {
    console.error('创建模拟异常:', err)
    alert(t('step1.createSimulationException', { error: err.message }))
  } finally {
    creatingSimulation.value = false
  }
}

// 从选择弹窗里点某个已有 simulation
const selectExistingSimulation = (sim) => {
  showSimulationPicker.value = false
  const route = resolveSimulationRoute(sim)
  if (route) {
    router.push(route)
  }
}

// 选择弹窗里每一行的日期显示
const formatPickerDate = (dateStr) => {
  if (!dateStr) return ''
  try {
    return new Date(dateStr).toLocaleString('en-US', { hour12: false })
  } catch {
    return dateStr
  }
}

// 选择弹窗里每一行的状态文字
const formatPickerStatus = (sim) => {
  const progress = getSimulationProgress(sim)
  if (progress === 'completed' && sim.report_id) return t('history.step4Button')
  if (progress === 'not-started') return t('history.notStarted')
  return t('history.roundsProgress', { current: sim.current_round || 0, total: sim.total_rounds || 0 })
}

const selectOntologyItem = (item, type) => {
  selectedOntologyItem.value = { ...item, itemType: type }
}

const graphStats = computed(() => {
  const nodes = props.graphData?.node_count || props.graphData?.nodes?.length || 0
  const edges = props.graphData?.edge_count || props.graphData?.edges?.length || 0
  const types = props.projectData?.ontology?.entity_types?.length || 0
  return { nodes, edges, types }
})

const formatDate = (dateStr) => {
  if (!dateStr) return '--:--:--'
  const d = new Date(dateStr)
  return d.toLocaleTimeString('en-US', { hour12: false }) + '.' + d.getMilliseconds()
}

// 自动检查：graph 已完成时自动看有没有已有 simulation，不用等用户点按钮
// 用 watch 而不是 onMounted：MainView 里 projectData 一开始是 ref(null)，
// 要等 loadProject() 异步跑完才会被赋值，child 的 onMounted 比这更早触发，
// 若用 onMounted 判断 status，最常见的场景（打开时状态就已是 graph_completed）
// 永远读到 null，条件永远为 false，功能等于没跑。
// immediate: true 是为了兼容 projectData 恰好已经就位的边界情况。
// autoCheckDone 保证只跑一次，避免 status 之后又变化时重复触发。
let autoCheckDone = false
watch(() => props.projectData?.status, (newStatus) => {
  if (autoCheckDone || newStatus !== 'graph_completed') {
    return
  }
  autoCheckDone = true
  autoCheckExistingSimulation()
}, { immediate: true })

// Auto-scroll logs
watch(() => props.systemLogs.length, () => {
  nextTick(() => {
    if (logContent.value) {
      logContent.value.scrollTop = logContent.value.scrollHeight
    }
  })
})
</script>

<style scoped>
.workbench-panel {
  height: 100%;
  background-color: #FAFAFA;
  display: flex;
  flex-direction: column;
  position: relative;
  overflow: hidden;
}

.scroll-container {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.step-card {
  background: #FFF;
  border-radius: 8px;
  padding: 20px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.04);
  border: 1px solid #EAEAEA;
  transition: all 0.3s ease;
  position: relative; /* For absolute overlay */
}

.step-card.active {
  border-color: #FF5722;
  box-shadow: 0 4px 12px rgba(255, 87, 34, 0.08);
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.step-info {
  display: flex;
  align-items: center;
  gap: 12px;
}

.step-num {
  font-family: 'JetBrains Mono', monospace;
  font-size: 20px;
  font-weight: 700;
  color: #E0E0E0;
}

.step-card.active .step-num,
.step-card.completed .step-num {
  color: #000;
}

.step-title {
  font-weight: 600;
  font-size: 14px;
  letter-spacing: 0.5px;
}

.badge {
  font-size: 10px;
  padding: 4px 8px;
  border-radius: 4px;
  font-weight: 600;
  text-transform: uppercase;
}

.badge.success { background: #E8F5E9; color: #2E7D32; }
.badge.processing { background: #FF5722; color: #FFF; }
.badge.accent { background: #FF5722; color: #FFF; }
.badge.pending { background: #F5F5F5; color: #999; }

.api-note {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: #999;
  margin-bottom: 8px;
}

.description {
  font-size: 12px;
  color: #666;
  line-height: 1.5;
  margin-bottom: 16px;
}

/* Step 01 Tags */
.tags-container {
  margin-top: 12px;
  transition: opacity 0.3s;
}

.tags-container.dimmed {
    opacity: 0.3;
    pointer-events: none;
}

.tag-label {
  display: block;
  font-size: 10px;
  color: #AAA;
  margin-bottom: 8px;
  font-weight: 600;
}

.tags-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.entity-tag {
  background: #F5F5F5;
  border: 1px solid #EEE;
  padding: 4px 10px;
  border-radius: 4px;
  font-size: 11px;
  color: #333;
  font-family: 'JetBrains Mono', monospace;
  transition: all 0.2s;
}

.entity-tag.clickable {
    cursor: pointer;
}

.entity-tag.clickable:hover {
    background: #E0E0E0;
    border-color: #CCC;
}

/* Ontology Detail Overlay */
.ontology-detail-overlay {
    position: absolute;
    top: 60px; /* Below header roughly */
    left: 20px;
    right: 20px;
    bottom: 20px;
    background: rgba(255, 255, 255, 0.98);
    backdrop-filter: blur(4px);
    z-index: 10;
    border: 1px solid #EAEAEA;
    box-shadow: 0 4px 20px rgba(0,0,0,0.05);
    border-radius: 6px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    animation: fadeIn 0.2s ease-out;
}

@keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }

.detail-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px 16px;
    border-bottom: 1px solid #EAEAEA;
    background: #FAFAFA;
}

.detail-title-group {
    display: flex;
    align-items: center;
    gap: 8px;
}

.detail-type-badge {
    font-size: 9px;
    font-weight: 700;
    color: #FFF;
    background: #000;
    padding: 2px 6px;
    border-radius: 2px;
    text-transform: uppercase;
}

.detail-name {
    font-size: 14px;
    font-weight: 700;
    font-family: 'JetBrains Mono', monospace;
}

.close-btn {
    background: none;
    border: none;
    font-size: 18px;
    color: #999;
    cursor: pointer;
    line-height: 1;
}

.close-btn:hover {
    color: #333;
}

.detail-body {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
}

.detail-desc {
    font-size: 12px;
    color: #444;
    line-height: 1.5;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 1px dashed #EAEAEA;
}

.detail-section {
    margin-bottom: 16px;
}

.section-label {
    display: block;
    font-size: 10px;
    font-weight: 600;
    color: #AAA;
    margin-bottom: 8px;
}

.attr-list, .conn-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
}

.attr-item {
    font-size: 11px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: baseline;
    padding: 4px;
    background: #F9F9F9;
    border-radius: 4px;
}

.attr-name {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 600;
    color: #000;
}

.attr-type {
    color: #999;
    font-size: 10px;
}

.attr-desc {
    color: #555;
    flex: 1;
    min-width: 150px;
}

.example-list {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
}

.example-tag {
    font-size: 11px;
    background: #FFF;
    border: 1px solid #E0E0E0;
    padding: 3px 8px;
    border-radius: 12px;
    color: #555;
}

.conn-item {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 11px;
    padding: 6px;
    background: #F5F5F5;
    border-radius: 4px;
    font-family: 'JetBrains Mono', monospace;
}

.conn-node {
    font-weight: 600;
    color: #333;
}

.conn-arrow {
    color: #BBB;
}

/* Step 02 Stats */
.stats-grid {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 12px;
  background: #F9F9F9;
  padding: 16px;
  border-radius: 6px;
}

.stat-card {
  text-align: center;
}

.stat-value {
  display: block;
  font-size: 20px;
  font-weight: 700;
  color: #000;
  font-family: 'JetBrains Mono', monospace;
}

.stat-label {
  font-size: 9px;
  color: #999;
  text-transform: uppercase;
  margin-top: 4px;
  display: block;
}

/* Step 03 Button */
.action-btn {
  width: 100%;
  background: #000;
  color: #FFF;
  border: none;
  padding: 14px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: opacity 0.2s;
}

.action-btn:hover:not(:disabled) {
  opacity: 0.8;
}

.action-btn:disabled {
  background: #CCC;
  cursor: not-allowed;
}

.progress-section {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  color: #FF5722;
  margin-bottom: 12px;
}

.spinner-sm {
  width: 14px;
  height: 14px;
  border: 2px solid #FFCCBC;
  border-top-color: #FF5722;
  border-radius: 50%;
  animation: spin 1s linear infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }

/* Step 03 Check-Existing Error */
.check-error-box {
  margin-top: 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  background: #FFF3F0;
  border: 1px solid #FFCCBC;
  border-radius: 4px;
}

.check-error-text {
  font-size: 11px;
  color: #D32F2F;
  flex: 1;
}

.retry-btn {
  background: #FF5722;
  color: #FFF;
  border: none;
  padding: 6px 12px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  flex-shrink: 0;
}

.retry-btn:hover {
  opacity: 0.85;
}

/* Simulation Picker Overlay */
.picker-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
  backdrop-filter: blur(4px);
}

.picker-content {
  background: #FFF;
  width: 480px;
  max-width: 90vw;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  border: 1px solid #EAEAEA;
  border-radius: 8px;
  box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1);
  overflow: hidden;
}

.picker-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 20px;
  border-bottom: 1px solid #EAEAEA;
}

.picker-title {
  font-size: 14px;
  font-weight: 700;
}

.picker-desc {
  padding: 0 20px;
  margin: 12px 0 0;
  font-size: 12px;
  color: #666;
}

.picker-list {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.picker-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
  background: #F9F9F9;
  border: 1px solid #EEE;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s;
  font-family: 'JetBrains Mono', monospace;
}

.picker-item:hover {
  border-color: #FF5722;
  background: #FFF3F0;
}

.picker-item-id {
  font-size: 11px;
  font-weight: 600;
  color: #333;
  flex-shrink: 0;
}

.picker-item-date {
  font-size: 10px;
  color: #999;
  flex: 1;
  text-align: center;
}

.picker-item-status {
  font-size: 10px;
  font-weight: 600;
  flex-shrink: 0;
}

.picker-item-status.completed { color: #10B981; }
.picker-item-status.in-progress { color: #F59E0B; }
.picker-item-status.not-started { color: #9CA3AF; }

.create-new-btn {
  margin: 0 20px 20px;
  background: #000;
  color: #FFF;
  border: none;
  padding: 12px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}

.create-new-btn:hover {
  opacity: 0.85;
}

/* System Logs */
.system-logs {
  background: #000;
  color: #DDD;
  padding: 16px;
  font-family: 'JetBrains Mono', monospace;
  border-top: 1px solid #222;
  flex-shrink: 0;
}

.log-header {
  display: flex;
  justify-content: space-between;
  border-bottom: 1px solid #333;
  padding-bottom: 8px;
  margin-bottom: 8px;
  font-size: 10px;
  color: #888;
}

.log-content {
  display: flex;
  flex-direction: column;
  gap: 4px;
  height: 80px; /* Approx 4 lines visible */
  overflow-y: auto;
  padding-right: 4px;
}

.log-content::-webkit-scrollbar {
  width: 4px;
}

.log-content::-webkit-scrollbar-thumb {
  background: #333;
  border-radius: 2px;
}

.log-line {
  font-size: 11px;
  display: flex;
  gap: 12px;
  line-height: 1.5;
}

.log-time {
  color: #666;
  min-width: 75px;
}

.log-msg {
  color: #CCC;
  word-break: break-all;
}
</style>
