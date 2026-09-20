import { computed } from 'vue'

/**
 * Mode viewer: dipakai saat halaman ini di-embed (iframe) dan alurnya digerakkan
 * server. Aktif lewat query `?viewer=1`. Tanpa param itu perilaku standalone
 * lama tidak berubah sama sekali.
 *
 * Sticky: sekali terdeteksi (di URL awal, atau di route.query lewat router guard)
 * mode tetap aktif selama sesi halaman, dan router guard membawa `viewer=1`
 * ke setiap navigasi berikutnya.
 */
const readFromLocation = () => {
  try {
    return new URLSearchParams(window.location.search).get('viewer') === '1'
  } catch {
    return false
  }
}

let sticky = readFromLocation()

/** Tandai root <html> supaya CSS responsif viewer bisa di-scope `.is-viewer` (non-viewer tak tersentuh). */
export const applyViewerRootClass = () => {
  try {
    document.documentElement.classList.toggle('is-viewer', sticky || readFromLocation())
  } catch {
    // DOM tidak tersedia
  }
}

export const isViewerMode = () => sticky || readFromLocation()

export const markViewerFromQuery = (query) => {
  if (query && query.viewer === '1' && !sticky) {
    sticky = true
    applyViewerRootClass()
  }
  return sticky
}

export const withViewerQuery = (location) => {
  if (!isViewerMode()) return location
  return { ...location, query: { ...(location.query || {}), viewer: '1' } }
}

/** Layout awal: di viewer pada layar sempit (ponsel) langsung Workbench, bukan split 50/50. */
export const defaultViewMode = (fallback = 'split') => {
  try {
    if (isViewerMode() && window.matchMedia('(max-width: 768px)').matches) return 'workbench'
  } catch {
    // matchMedia tidak tersedia: pakai default
  }
  return fallback
}

/** Composable: nilai konstan per sesi halaman (tidak reaktif ke URL). */
export const useViewerMode = () => {
  const viewer = computed(() => isViewerMode())
  return { viewer }
}
