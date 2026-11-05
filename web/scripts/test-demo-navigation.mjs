import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse } from '@vue/compiler-sfc'
import { compile } from '@vue/compiler-dom'
import * as Vue from 'vue'
import { createMemoryHistory, createRouter, useRoute } from 'vue-router'
import { renderToString } from '@vue/server-renderer'

const source = await readFile(new URL('../src/App.vue', import.meta.url), 'utf8')
const template = parse(source).descriptor.template.content
const match = template.match(/<n-message-provider>([\s\S]*?)<\/n-message-provider>/)
assert.ok(match, 'App.vue must contain its route rendering block')
const { code } = compile(`<n-message-provider>${match[1]}</n-message-provider>`, { mode: 'function' })
const render = new Function('Vue', code)(Vue)

const wrapper = { setup(_, { slots }) { return () => Vue.h('div', slots.default?.()) } }
const demoConnections = { render: () => Vue.h('span', 'DEMO_CONNECTIONS') }
const view = (text) => ({ render: () => Vue.h('span', text) })

test('demo navigation keeps positions and backtest separate from CEX credentials', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/positions', component: view('POSITIONS_VIEW') },
    { path: '/cex', component: view('CEX_VIEW') },
    { path: '/backtest', component: view('BACKTEST_VIEW') },
  ] })
  const app = Vue.createSSRApp({ setup: () => ({ route: useRoute(), executionMode: 'demo', isDemoMode: false }), render })
  app.component('n-message-provider', wrapper)
  app.component('n-text', wrapper)
  app.component('DemoConnections', demoConnections)
  app.use(router)
  for (const [path, expected, absent] of [
    ['/positions', 'POSITIONS_VIEW', 'DEMO_CONNECTIONS'],
    ['/cex', 'DEMO_CONNECTIONS', 'CEX_VIEW'],
    ['/backtest', 'BACKTEST_VIEW', 'DEMO_CONNECTIONS'],
    ['/positions', 'POSITIONS_VIEW', 'DEMO_CONNECTIONS'],
  ]) {
    await router.push(path)
    await router.isReady()
    const html = await renderToString(app)
    assert.ok(html.includes(expected), `${path}: ${html}`)
    assert.ok(!html.includes(absent), `${path}: ${html}`)
  }
})
