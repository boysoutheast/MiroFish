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

export const isViewerMode = () => sticky || readFromLocation()

export const markViewerFromQuery = (query) => {
  if (query && query.viewer === '1') sticky = true
  return sticky
}

export const withViewerQuery = (location) => {
  if (!isViewerMode()) return location
  return { ...location, query: { ...(location.query || {}), viewer: '1' } }
}

/** Composable: nilai konstan per sesi halaman (tidak reaktif ke URL). */
export const useViewerMode = () => {
  const viewer = computed(() => isViewerMode())
  return { viewer }
}
