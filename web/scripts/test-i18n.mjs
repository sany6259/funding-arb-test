import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { build } from 'esbuild'

const locales = ['en', 'ru', 'zh-CN', 'zh-TW']
const dictionaries = Object.fromEntries(await Promise.all(locales.map(async (locale) => [
  locale, JSON.parse(await readFile(new URL(`../src/locales/${locale}.json`, import.meta.url), 'utf8')),
])))

function flatten(value, prefix = '', output = {}) {
  for (const [key, item] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (typeof item === 'string') output[path] = item
    else flatten(item, path, output)
  }
  return output
}

const reference = flatten(dictionaries.en)
const params = (message) => [...message.matchAll(/\{([\w]+)\}/g)].map((match) => match[1]).sort()

test('all languages have the same keys and interpolation variables', () => {
  for (const locale of locales) {
    const actual = flatten(dictionaries[locale])
    assert.deepEqual(Object.keys(actual).sort(), Object.keys(reference).sort(), locale)
    for (const key of Object.keys(reference)) {
      assert.deepEqual(params(actual[key]), params(reference[key]), `${locale}: ${key}`)
    }
  }
})

async function bundle(entry) {
  const result = await build({
    entryPoints: [new URL(entry, import.meta.url).pathname],
    bundle: true, platform: 'node', format: 'esm', write: false,
  })
  return result.outputFiles[0].text
}

const i18nCode = await bundle('../src/i18n.ts')
let version = 0
async function loadI18n() {
  const encoded = Buffer.from(i18nCode).toString('base64')
  return import(`data:text/javascript;base64,${encoded}#${version++}`)
}
function storage(initial = null) {
  let value = initial
  return {
    getItem(key) { assert.equal(key, 'locale'); return value },
    setItem(key, next) { assert.equal(key, 'locale'); value = next },
    value() { return value },
  }
}

test('default, preserved, and invalid saved languages', async () => {
  for (const [saved, expected] of [[null, 'ru'], ['ru', 'ru'], ['en', 'en'], ['zh-CN', 'zh-CN'], ['zh-TW', 'zh-TW'], ['invalid', 'ru'], ['', 'ru']]) {
    globalThis.localStorage = storage(saved)
    const { default: i18n } = await loadI18n()
    assert.equal(i18n.global.locale.value, expected)
    if (saved !== null && saved !== expected) assert.equal(globalThis.localStorage.value(), expected)
  }
})

test('selection persists and reloads, and storage failures do not prevent switching', async () => {
  globalThis.localStorage = storage()
  const first = await loadI18n()
  for (const locale of locales) {
    first.setLocale(locale)
    assert.equal(first.default.global.locale.value, locale)
    assert.equal(globalThis.localStorage.value(), locale)
    assert.equal((await loadI18n()).default.global.locale.value, locale)
  }
  globalThis.localStorage = { getItem() { throw Error('blocked') }, setItem() { throw Error('blocked') } }
  const blocked = await loadI18n()
  assert.equal(blocked.default.global.locale.value, 'ru')
  blocked.setLocale('en')
  assert.equal(blocked.default.global.locale.value, 'en')
  delete globalThis.localStorage
})

test('Russian documentation falls back to English, not Chinese', async () => {
  const code = await bundle('../src/content/docs/index.ts')
  const docs = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)
  for (const article of docs.DOC_ARTICLES) {
    assert.ok(article.sectionsByLocale.en?.length, article.slug)
    assert.equal(docs.getDocSections(article, 'ru'), article.sectionsByLocale.ru ?? article.sectionsByLocale.en)
  }
})


test('all messages compile and interpolate in vue-i18n', async () => {
  globalThis.localStorage = storage()
  const { default: i18n } = await loadI18n()
  const originalError = console.error
  const errors = []
  console.error = (...args) => errors.push(args.join(' '))
  try {
    for (const locale of locales) {
      i18n.global.locale.value = locale
      for (const key of Object.keys(reference)) {
        const values = Object.fromEntries(params(reference[key]).map((name) => [name, 'VALUE']))
        const message = i18n.global.t(key, values)
        assert.notEqual(message, key, `${locale}: ${key}`)
        assert.ok(!/\{[\w]+\}/.test(message), `${locale}: ${key}`)
      }
    }
    assert.deepEqual(errors, [])
  } finally {
    console.error = originalError
    delete globalThis.localStorage
  }
})
