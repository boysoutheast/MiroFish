import { createRouter, createWebHistory } from 'vue-router'
import Home from '../views/Home.vue'
import Process from '../views/MainView.vue'
import SimulationView from '../views/SimulationView.vue'
import SimulationRunView from '../views/SimulationRunView.vue'
import ReportView from '../views/ReportView.vue'
import InteractionView from '../views/InteractionView.vue'
import { markViewerFromQuery } from '../utils/viewerMode'

const routes = [
  {
    path: '/',
    name: 'Home',
    component: Home
  },
  {
    path: '/process/:projectId',
    name: 'Process',
    component: Process,
    props: true
  },
  {
    path: '/simulation/:simulationId',
    name: 'Simulation',
    component: SimulationView,
    props: true
  },
  {
    path: '/simulation/:simulationId/start',
    name: 'SimulationRun',
    component: SimulationRunView,
    props: true
  },
  {
    path: '/report/:reportId',
    name: 'Report',
    component: ReportView,
    props: true
  },
  {
    path: '/interaction/:reportId',
    name: 'Interaction',
    component: InteractionView,
    props: true
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

// Mode viewer: bawa `?viewer=1` ke setiap navigasi supaya tidak hilang di tengah alur.
router.beforeEach((to) => {
  // Viewer read-only: layar chat/survei (Step 5) memicu panggilan LLM, jadi tidak boleh dibuka.
  if (markViewerFromQuery(to.query) && to.name === 'Interaction') {
    return { name: 'Report', params: { reportId: to.params.reportId }, query: { viewer: '1' }, replace: true }
  }
  if (markViewerFromQuery(to.query) && to.query.viewer !== '1') {
    return { path: to.path, query: { ...to.query, viewer: '1' }, hash: to.hash, replace: true }
  }
  return true
})

export default router
