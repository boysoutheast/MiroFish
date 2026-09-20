// Bandingkan himpunan kunci (dan placeholder {x}) locales/en.json vs id.json. Exit 1 bila beda.
import { readFileSync } from 'node:fs'
const load = (n) => JSON.parse(readFileSync(new URL(`../locales/${n}.json`, import.meta.url), 'utf8'))
const flat = (o, p = '', out = {}) => {
  for (const [k, v] of Object.entries(o)) {
    const key = p ? `${p}.${k}` : k
    if (v && typeof v === 'object') flat(v, key, out)
    else out[key] = v
  }
  return out
}
const ph = (s) => (String(s).match(/\{[^}]+\}/g) || []).sort().join(',')
const en = flat(load('en')), id = flat(load('id'))
const missing = Object.keys(en).filter((k) => !(k in id))
const extra = Object.keys(id).filter((k) => !(k in en))
const phDiff = Object.keys(en).filter((k) => k in id && ph(en[k]) !== ph(id[k]))
const banned = Object.entries(id).filter(([, v]) => /mirofish|[一-鿿]/i.test(String(v)))
console.log({ enKeys: Object.keys(en).length, idKeys: Object.keys(id).length, missing, extra, phDiff, banned: banned.map(([k]) => k) })
process.exit(missing.length || extra.length || phDiff.length || banned.length ? 1 : 0)
