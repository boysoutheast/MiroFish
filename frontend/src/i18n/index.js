import { createI18n } from 'vue-i18n'
import { isViewerMode } from '../utils/viewerMode'
import languages from '../../../locales/languages.json'

const localeFiles = import.meta.glob('../../../locales/!(languages).json', { eager: true })

const messages = {}
const availableLocales = []

for (const path in localeFiles) {
  const key = path.match(/\/([^/]+)\.json$/)[1]
  if (languages[key]) {
    messages[key] = localeFiles[path].default
    availableLocales.push({ key, label: languages[key].label })
  }
}

// Viewer (?viewer=1): default Indonesia bila pengguna belum memilih bahasa; non-viewer tetap 'en'.
const readSavedLocale = () => {
  try {
    return localStorage.getItem('locale')
  } catch {
    return null
  }
}
const savedLocale = readSavedLocale() || (isViewerMode() && messages.id ? 'id' : 'en')

const i18n = createI18n({
  legacy: false,
  locale: savedLocale,
  fallbackLocale: 'en',
  messages
})

export { availableLocales }
export default i18n
