<template>
  <div class="main-view">
    <!-- Header -->
    <header class="app-header">
      <div class="header-left"></div>

      <div class="header-center">
        <div class="view-switcher">
          <button 
            v-for="mode in ['graph', 'split', 'workbench']" 
            :key="mode"
            class="switch-btn"
            :class="{ active: viewMode === mode }"
            @click="viewMode = mode"
          >
            {{ { graph: $t('main.layoutGraph'), split: $t('main.layoutSplit'), workbench: $t('main.layoutWorkbench') }[mode] }}
          </button>
        </div>
      </div>

      <div class="header-right">
        <LanguageSwitcher />
        <div class="step-divider"></div>
        <div class="workflow-step">
          <span class="step-num">Step 2/5</span>
          <span class="step-name">{{ $tm('main.stepNames')[1] }}</span>
        </div>
        <div class="step-divider"></div>
        <span class="status-indicator" :class="statusClass">
          <span class="dot"></span>
          {{ statusText }}
        </span>
      </div>
    </header>

    <!-- 检测到有正在运行的模拟：给一个显式的横条，而不是一进来就自动 stop -->
    <div v-if="runningSimDetected" class="running-sim-banner">
      <span class="banner-text">{{ $t('step2.runningSimDetectedBanner') }}</span>
      <div class="banner-actions">
        <router-link
          class="banner-link"
          :to="{ name: 'SimulationRun', params: { simulationId: currentSimulationId } }"
        >
          {{ $t('step2.continueToStep3') }}
        </router-link>
        <button class="banner-stop-btn" :disabled="isStoppingRunningSim" @click="handleStopRunningSimClick">
          {{ $t('step2.stopRunningSimBtn') }}
        </button>
      </div>
    </div>

    <!-- redirect 判定中：不渲染主内容区，避免 Step2 闪现 + Step2EnvSetup 提前挂载打 API -->
    <div v-if="isCheckingRedirect" class="redirect-checking">
      {{ $t('common.loading') }}
    </div>

    <!-- Main Content Area -->
    <main v-else class="content-area">
      <!-- Left Panel: Graph -->
      <div class="panel-wrapper left" :style="leftPanelStyle">
        <GraphPanel
          :graphData="graphData"
          :loading="graphLoading"
          :currentPhase="2"
          @refresh="refreshGraph"
          @toggle-maximize="toggleMaximize('graph')"
        />
      </div>

      <!-- Right Panel: Step2 环境搭建 -->
      <div class="panel-wrapper right" :style="rightPanelStyle">
        <Step2EnvSetup
          v-if="!isViewerMode()"
          :simulationId="currentSimulationId"
          :projectData="projectData"
          :graphData="graphData"
          :systemLogs="systemLogs"
          @go-back="handleGoBack"
          @next-step="handleNextStep"
          @add-log="addLog"
          @update-status="updateStatus"
        />
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import GraphPanel from '../components/GraphPanel.vue'
import Step2EnvSetup from '../components/Step2EnvSetup.vue'
import { getProject, getGraphData } from '../api/graph'
import { isViewerMode } from '../utils/viewerMode'
import { getSimulation, stopSimulation, getEnvStatus, closeSimulationEnv } from '../api/simulation'
import LanguageSwitcher from '../components/LanguageSwitcher.vue'
import { useI18n } from 'vue-i18n'
import { getSimulationProgress, isRunnerAlive } from '../utils/simulationProgress'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

// Props
const props = defineProps({
  simulationId: String
})

// Layout State
const viewMode = ref('split')

// Data State
const currentSimulationId = ref(route.params.simulationId)
const projectData = ref(null)
const graphData = ref(null)
const graphLoading = ref(false)
const systemLogs = ref([])
const currentStatus = ref('processing') // processing | completed | error
const runningSimDetected = ref(false) // Step2 挂载时检测到有模拟还在跑（只读检测，不自动停）
const isStoppingRunningSim = ref(false) // 横条 Stop 按钮的 busy 状态，防止连点两次触发两轮 stop
const isCheckingRedirect = ref(true) // redirect 判定进行中：主内容区（含 Step2EnvSetup）先不挂载，避免闪现 + 提前打 API

// --- Computed Layout Styles ---
const leftPanelStyle = computed(() => {
  if (viewMode.value === 'graph') return { width: '100%', opacity: 1, transform: 'translateX(0)' }
  if (viewMode.value === 'workbench') return { width: '0%', opacity: 0, transform: 'translateX(-20px)' }
  return { width: '50%', opacity: 1, transform: 'translateX(0)' }
})

const rightPanelStyle = computed(() => {
  if (viewMode.value === 'workbench') return { width: '100%', opacity: 1, transform: 'translateX(0)' }
  if (viewMode.value === 'graph') return { width: '0%', opacity: 0, transform: 'translateX(20px)' }
  return { width: '50%', opacity: 1, transform: 'translateX(0)' }
})

// --- Status Computed ---
const statusClass = computed(() => {
  return currentStatus.value
})

const statusText = computed(() => {
  if (currentStatus.value === 'error') return 'Error'
  if (currentStatus.value === 'completed') return 'Ready'
  return 'Preparing'
})

// --- Helpers ---
const addLog = (msg) => {
  const time = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' }) + '.' + new Date().getMilliseconds().toString().padStart(3, '0')
  systemLogs.value.push({ time, msg })
  if (systemLogs.value.length > 100) {
    systemLogs.value.shift()
  }
}

const updateStatus = (status) => {
  currentStatus.value = status
}

// --- Layout Methods ---
const toggleMaximize = (target) => {
  if (viewMode.value === target) {
    viewMode.value = 'split'
  } else {
    viewMode.value = target
  }
}

const handleGoBack = () => {
  if (isViewerMode()) return
  // 返回到 process 页面
  if (projectData.value?.project_id) {
    router.push({ name: 'Process', params: { projectId: projectData.value.project_id } })
  } else {
    router.push('/')
  }
}

const handleNextStep = (params = {}) => {
  if (isViewerMode()) return
  addLog(t('log.enterStep3'))

  // 记录模拟轮数配置
  if (params.maxRounds) {
    addLog(t('log.customRoundsConfig', { rounds: params.maxRounds }))
  } else {
    addLog(t('log.useAutoRounds'))
  }
  
  // 构建路由参数
  const routeParams = {
    name: 'SimulationRun',
    params: { simulationId: currentSimulationId.value }
  }
  
  // 如果有自定义轮数，通过 query 参数传递
  if (params.maxRounds) {
    routeParams.query = { maxRounds: params.maxRounds }
  }
  
  // 跳转到 Step 3 页面
  router.push(routeParams)
}

// --- Data Logic ---

/**
 * 只读检测：有没有模拟还在跑（不停止任何东西）。
 * 用来决定要不要显示 "Stop Running Simulation" 横条——
 * 停不停交给用户点显式按钮，不再一进 Step2 就自动停。
 */
const detectRunningSimulation = async () => {
  if (!currentSimulationId.value) return

  try {
    const envStatusRes = await getEnvStatus({ simulation_id: currentSimulationId.value })
    if (envStatusRes.success && envStatusRes.data?.env_alive) {
      runningSimDetected.value = true
      return
    }

    const simRes = await getSimulation(currentSimulationId.value)
    if (simRes.success && simRes.data?.status === 'running') {
      runningSimDetected.value = true
    }
  } catch (err) {
    // 检测失败 ≠ 没有模拟在跑，是 UNKNOWN——不能悄悄吞掉，用户得知道
    // 横条判断可能不准，需要手动 refresh 再确认。
    console.warn('检查模拟运行状态失败:', err)
    addLog(t('log.detectRunningSimFailed', { error: err.message }))
  }
}

/**
 * 点击横条上的 Stop 按钮才会真正停止——由用户显式触发。
 */
const handleStopRunningSimClick = async () => {
  if (isStoppingRunningSim.value) return
  if (!confirm(t('log.confirmStopSimulation'))) return

  isStoppingRunningSim.value = true
  try {
    await checkAndStopRunningSimulation()
    runningSimDetected.value = false
  } finally {
    isStoppingRunningSim.value = false
  }
}

/**
 * 检查并关闭正在运行的模拟——现在只在用户点击 Stop 横条按钮时调用，
 * 不再挂载即自动执行（那会把用户还想继续的模拟直接停掉）。
 */
const checkAndStopRunningSimulation = async () => {
  if (!currentSimulationId.value) return

  try {
    // 先检查模拟环境是否存活
    const envStatusRes = await getEnvStatus({ simulation_id: currentSimulationId.value })
    
    if (envStatusRes.success && envStatusRes.data?.env_alive) {
      addLog(t('log.detectedSimEnvRunning'))
      
      // 尝试优雅关闭模拟环境
      try {
        const closeRes = await closeSimulationEnv({ 
          simulation_id: currentSimulationId.value,
          timeout: 10  // 10秒超时
        })
        
        if (closeRes.success) {
          addLog(t('log.simEnvClosed'))
        } else {
          addLog(t('log.closeSimEnvFailedWithError', { error: closeRes.error || t('common.unknownError') }))
          // 如果优雅关闭失败，尝试强制停止
          await forceStopSimulation()
        }
      } catch (closeErr) {
        addLog(t('log.closeSimEnvException', { error: closeErr.message }))
        // 如果优雅关闭异常，尝试强制停止
        await forceStopSimulation()
      }
    } else {
      // 环境未运行，但可能进程还在，检查模拟状态
      const simRes = await getSimulation(currentSimulationId.value)
      if (simRes.success && simRes.data?.status === 'running') {
        addLog(t('log.detectedSimRunning'))
        await forceStopSimulation()
      }
    }
  } catch (err) {
    // 检查环境状态失败不影响后续流程
    console.warn('检查模拟状态失败:', err)
  }
}

/**
 * 强制停止模拟
 */
const forceStopSimulation = async () => {
  try {
    const stopRes = await stopSimulation({ simulation_id: currentSimulationId.value })
    if (stopRes.success) {
      addLog(t('log.simForceStopSuccess'))
    } else {
      addLog(t('log.forceStopSimFailed', { error: stopRes.error || t('common.unknownError') }))
    }
  } catch (err) {
    addLog(t('log.forceStopSimException', { error: err.message }))
  }
}

const loadSimulationData = async () => {
  try {
    addLog(t('log.loadingSimData', { id: currentSimulationId.value }))

    // 获取 simulation 信息
    const simRes = await getSimulation(currentSimulationId.value)
    if (simRes.success && simRes.data) {
      const simData = simRes.data

      // 获取 project 信息
      if (simData.project_id) {
        const projRes = await getProject(simData.project_id)
        if (projRes.success && projRes.data) {
          projectData.value = projRes.data
          addLog(t('log.projectLoadSuccess', { id: projRes.data.project_id }))
          
          // 获取 graph 数据
          if (projRes.data.graph_id) {
            await loadGraph(projRes.data.graph_id)
          }
        }
      }
    } else {
      addLog(t('log.loadSimDataFailed', { error: simRes.error || t('common.unknownError') }))
    }
  } catch (err) {
    addLog(t('log.loadException', { error: err.message }))
  }
}

const loadGraph = async (graphId) => {
  graphLoading.value = true
  try {
    const res = await getGraphData(graphId)
    if (res.success) {
      graphData.value = res.data
      addLog(t('log.graphDataLoadSuccess'))
    }
  } catch (err) {
    addLog(t('log.graphLoadFailed', { error: err.message }))
  } finally {
    graphLoading.value = false
  }
}

const refreshGraph = () => {
  if (projectData.value?.graph_id) {
    loadGraph(projectData.value.graph_id)
  }
}

/**
 * 根据模拟真实状态决定要不要把用户从 Step2 转走。
 * - 已有 report_id → 直接去 Report，跳过所有中间步骤
 * - 没有 report 但在跑/跑过一半 → 去 Step3（SimulationRun），别拽回 Step2
 * - 真的还没开始 → 留在 Step2，行为不变
 * 检测失败（网络/异常）时不猜测，留在 Step2（旧行为），只记录日志。
 * @returns {boolean} true 表示已经 redirect 走了，调用方不该再继续走 Step2 的初始化
 */
const redirectByRealStatus = async () => {
  if (!currentSimulationId.value) {
    isCheckingRedirect.value = false
    return false
  }

  try {
    const simRes = await getSimulation(currentSimulationId.value)
    if (!simRes.success || !simRes.data) {
      isCheckingRedirect.value = false
      return false
    }

    const simData = simRes.data

    if (simData.report_id) {
      router.push({ name: 'Report', params: { reportId: simData.report_id } })
      return true
    }

    if (
      getSimulationProgress(simData) === 'in-progress' ||
      getSimulationProgress(simData) === 'completed' ||
      isRunnerAlive(simData.runner_status)
    ) {
      router.push({ name: 'SimulationRun', params: { simulationId: currentSimulationId.value } })
      return true
    }

    isCheckingRedirect.value = false
    return false
  } catch (err) {
    // 检测异常 ≠ 该转走，留在 Step2 是安全的旧行为，不能拿错误当路由信号
    console.warn('检查模拟真实状态失败，留在 Step2:', err)
    addLog(t('log.detectRunningSimFailed', { error: err.message }))
    isCheckingRedirect.value = false
    return false
  }
}

onMounted(async () => {
  addLog(t('log.simViewInit'))

  // Mode viewer: Step 2 (prepare/start) dikerjakan server — tidak pernah dirender di sini.
  // Redirect DULU, isCheckingRedirect tetap true sehingga Step2EnvSetup tidak sempat terpasang.
  if (isViewerMode()) {
    router.replace({ name: 'SimulationRun', params: { simulationId: currentSimulationId.value } })
    return
  }

  // 先确认真实状态，该转走的（已有报告 / 正在跑）在这里就转走，别让 Step2 先渲染出来
  const redirected = await redirectByRealStatus()
  if (redirected) return

  // 只读检测有没有模拟还在跑，决定要不要显示横条——不自动停任何东西
  await detectRunningSimulation()

  // 加载模拟数据
  loadSimulationData()
})
</script>

<style scoped>
.main-view {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: #FFF;
  overflow: hidden;
  font-family: 'Space Grotesk', 'Noto Sans SC', system-ui, sans-serif;
}

/* Header */
.app-header {
  height: 60px;
  border-bottom: 1px solid #EAEAEA;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  background: #FFF;
  z-index: 100;
  position: relative;
}


.header-center {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
}

.view-switcher {
  display: flex;
  background: #F5F5F5;
  padding: 4px;
  border-radius: 6px;
  gap: 4px;
}

.switch-btn {
  border: none;
  background: transparent;
  padding: 6px 16px;
  font-size: 12px;
  font-weight: 600;
  color: #666;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.2s;
}

.switch-btn.active {
  background: #FFF;
  color: #000;
  box-shadow: 0 2px 4px rgba(0,0,0,0.05);
}

.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}

.workflow-step {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
}

.step-num {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 700;
  color: #999;
}

.step-name {
  font-weight: 700;
  color: #000;
}

.step-divider {
  width: 1px;
  height: 14px;
  background-color: #E0E0E0;
}

.status-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #666;
  font-weight: 500;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #CCC;
}

.status-indicator.processing .dot { background: #FF5722; animation: pulse 1s infinite; }
.status-indicator.completed .dot { background: #4CAF50; }
.status-indicator.error .dot { background: #F44336; }

@keyframes pulse { 50% { opacity: 0.5; } }

.redirect-checking {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #999;
  font-size: 13px;
}

/* Content */
.content-area {
  flex: 1;
  display: flex;
  position: relative;
  overflow: hidden;
}

.panel-wrapper {
  height: 100%;
  overflow: hidden;
  transition: width 0.4s cubic-bezier(0.25, 0.8, 0.25, 1), opacity 0.3s ease, transform 0.3s ease;
  will-change: width, opacity, transform;
}

.panel-wrapper.left {
  border-right: 1px solid #EAEAEA;
}

.running-sim-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 24px;
  background: #FFF7E6;
  border-bottom: 1px solid #FFE0A3;
  font-size: 13px;
  color: #333;
}

.banner-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
}

.banner-link {
  font-weight: 700;
  color: #000;
  text-decoration: underline;
}

.banner-stop-btn {
  border: 1px solid #DDD;
  background: #FFF;
  padding: 4px 12px;
  border-radius: 4px;
  font-size: 12px;
  cursor: pointer;
}

.banner-stop-btn:hover:not(:disabled) {
  background: #F5F5F5;
}

.banner-stop-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>

