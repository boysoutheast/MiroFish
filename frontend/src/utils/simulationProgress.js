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
