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
 * @param {string} runnerStatus
 * @returns {boolean}
 */
export function isRunnerAlive(runnerStatus) {
  return runnerStatus === 'running' || runnerStatus === 'paused' || runnerStatus === 'stopping'
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
