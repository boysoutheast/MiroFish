/**
 * 模拟进度 / 运行状态的共享语义 helper。
 *
 * 抽出来的原因：HistoryDatabase.vue 的进度徽标（getProgressClass）和
 * Step3Simulation.vue 的 attach-first 判定都需要判断 "模拟是不是还活着 /
 * 进度到哪了"，逻辑必须保持完全一致，否则一边显示"进行中"另一边又走了
 * 重新开始的分支。
 */

/**
 * 根据轮数计算模拟的进度状态。
 * @param {{ current_round?: number, total_rounds?: number }} sim
 * @returns {'not-started' | 'in-progress' | 'completed'}
 */
export function getSimulationProgress(sim) {
  const current = sim?.current_round || 0
  const total = sim?.total_rounds || 0

  if (total === 0 || current === 0) {
    return 'not-started'
  } else if (current >= total) {
    return 'completed'
  } else {
    return 'in-progress'
  }
}

/**
 * 判断后端 runner_status 是否代表"进程还活着"（可以 attach 上去继续轮询），
 * 而不需要重新 start。
 *
 * 'crashed' 算进来是有意的（T3F2）：进程本身死了，但后端正在自动重试同一个
 * simulation_id，不是一个可以重新 start 的 idle 状态——attach 上去继续轮询
 * 才能等到重试的结果，重新 start 只会跟后端的自动重试打架。
 * 'needs_attention' 不算——那是重试也失败后的终态，不会自己恢复，不属于
 * "还活着"；它在 Step3Simulation.vue 的 onMounted 里单独判断是否要 attach
 * （为了显示终态提示，而不是盲目重新 start），语义上跟这里的"活着"不一样。
 *
 * 这个函数被两个地方共用：Step3Simulation.vue（判断是否 attach 而非重新
 * start）以及 SimulationView.vue（判断要不要把用户带去 Step3/SimulationRun
 * 而不是 Step2）。两处都是刻意让 'crashed' 算"活着"——vue-reviewer 已核实
 * SimulationView.vue 那边因此把 crashed 也带去 run 页面是有意行为、非分叉
 * （"no divergence introduced"），不是需要修的 bug。以后改这个函数前，先确认
 * 两个调用点都还需要一致的语义。
 * @param {string} runnerStatus
 * @returns {boolean}
 */
export function isRunnerAlive(runnerStatus) {
  return runnerStatus === 'running' || runnerStatus === 'paused' || runnerStatus === 'stopping' || runnerStatus === 'crashed'
}

/**
 * 根据模拟进度决定"点开这个 simulation 该跳到哪个路由"的共享逻辑。
 * 3-way：not-started -> Simulation(Step2)，completed 且有 report_id -> Report(Step4)，
 * 其余（in-progress，或 completed 但还没生成报告）-> SimulationRun(Step3)。
 *
 * HistoryDatabase.vue 和 Step1GraphBuild.vue 都需要这个判定，抽出来避免两边逻辑分叉
 * （历史教训：曾经一边只看 report_id 两分支，导致进行中的模拟被错误带回 Step2）。
 * @param {{ simulation_id?: string, report_id?: string, total_rounds?: number }} sim
 * @returns {{ name: string, params: object, query?: object } | null} 路由对象，sim 缺少 simulation_id 时返回 null
 */
export function resolveSimulationRoute(sim) {
  if (!sim?.simulation_id) return null

  const progress = getSimulationProgress(sim)

  if (progress === 'not-started') {
    return { name: 'Simulation', params: { simulationId: sim.simulation_id } }
  }

  if (progress === 'completed' && sim.report_id) {
    return { name: 'Report', params: { reportId: sim.report_id } }
  }

  // in-progress，或 completed 但还没生成报告：回到 Step3 继续/收尾
  const route = { name: 'SimulationRun', params: { simulationId: sim.simulation_id } }
  if (sim.total_rounds) {
    route.query = { maxRounds: sim.total_rounds }
  }
  return route
}
