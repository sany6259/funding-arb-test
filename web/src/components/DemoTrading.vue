<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { NAlert, NButton, NCard, NFormItem, NInput, NInputNumber, NSelect, NSpace, NText } from 'naive-ui'

const localAccess = ['localhost', '127.0.0.1', '[::1]', '::1'].includes(window.location.hostname)
const base = ref('BTC')
const amount = ref('100')
const longVenue = ref('bybit')
const cancelVenue = ref('bybit')
const singleVenue = ref('bybit')
const singleSide = ref('buy')
const clientId = ref('')
const orderPermission = ref(false)
const openings = ref(false)
const status = ref('')
const output = ref('')
const busy = ref(false)
const strategyRunning = ref(false)
const strategyState = ref('')
const strategy = ref({ trade_usd: '1000', min_spread_pct: '0.04', min_net_pct: '0.02',
  exit_spread_pct: '0', max_mark_spread_pct: '0.2', max_leverage: '3', max_fee_pct: '0.1',
  max_slippage_pct: '0.1', max_funding_gap_sec: 600, interval_sec: 300 })
const venues = [{ label: 'Bybit Demo', value: 'bybit' }, { label: 'OKX Demo', value: 'okx' }]

async function refreshPermission() {
  try {
    const response = await fetch('/api/execution-mode')
    const data = await response.json()
    if (!response.ok || data.mode !== 'demo' || typeof data.orders_enabled !== 'boolean' || typeof data.openings_enabled !== 'boolean') throw new Error()
    orderPermission.value = data.orders_enabled
    openings.value = data.openings_enabled
    const strategyResponse = await fetch('/api/demo/trading/strategy')
    if (!strategyResponse.ok) throw new Error()
    const strategyData = await strategyResponse.json()
    strategyRunning.value = strategyData.running === true
    strategyState.value = JSON.stringify({ last: strategyData.last, error: strategyData.error }, null, 2)
  } catch {
    orderPermission.value = false
    openings.value = false
    strategyRunning.value = false
    status.value = 'Состояние режима недоступно.'
  }
}
let permissionTimer: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  refreshPermission()
  if (localAccess) fetch('/api/demo/trading/strategy/settings').then(async response => {
    if (!response.ok) throw new Error()
    const data = await response.json()
    if (data.settings !== null) strategy.value = data.settings
  }).catch(() => { status.value = 'Не удалось загрузить настройки стратегии.' })
  permissionTimer = setInterval(refreshPermission, 5000)
})
onUnmounted(() => { if (permissionTimer) clearInterval(permissionTimer) })

async function showJournal() {
  if (!localAccess || busy.value) return
  busy.value = true
  output.value = ''
  try {
    const [response, cancelResponse] = await Promise.all([
      fetch('/api/demo/trading/journal'), fetch('/api/demo/trading/cancel-intent'),
    ])
    const data = await response.json()
    const cancellation = await cancelResponse.json()
    if (!response.ok || !cancelResponse.ok) throw new Error(data.error || cancellation.error || 'Журнал недоступен.')
    output.value = JSON.stringify({ position: data.result, cancellation: cancellation.result }, null, 2)
    status.value = data.result === null && cancellation.result === null ? 'Журнал отсутствует.' : 'Данные журнала показаны ниже; это не подтверждение текущего состояния биржи.'
  } catch (error) {
    status.value = error instanceof Error ? error.message : 'Журнал недоступен.'
  } finally {
    busy.value = false
  }
}

async function showStrategyEvents() {
  if (!localAccess || busy.value) return
  busy.value = true
  output.value = ''
  try {
    const response = await fetch('/api/demo/trading/strategy/events')
    const data = await response.json()
    if (!response.ok) throw new Error(data.error || 'Журнал недоступен.')
    output.value = JSON.stringify(data.events, null, 2)
    status.value = 'Показаны последние события стратегии.'
  } catch (error) {
    status.value = error instanceof Error ? error.message : 'Журнал недоступен.'
  } finally { busy.value = false }
}

async function action(path: string, body: object, confirmation?: string) {
  if (!localAccess || busy.value || (confirmation && !window.confirm(confirmation))) return
  busy.value = true
  status.value = ''
  output.value = ''
  try {
    const response = await fetch(`/api/demo/trading/${path}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.error || 'Операция не выполнена.')
    output.value = JSON.stringify(data.result ?? data, null, 2)
    const state = data.result?.state
    status.value = typeof state === 'string' && state.includes('reconciliation_required')
      ? 'Результат неоднозначен: новые действия заблокированы до ручной сверки.' : 'Ответ получен. Проверьте состояние ниже.'
  } catch (error) {
    status.value = error instanceof Error ? error.message : 'Операция не выполнена.'
  } finally {
    await refreshPermission()
    busy.value = false
  }
}
</script>

<template>
  <n-space vertical>
    <n-alert type="warning" title="Bybit + OKX Demo">
      Демо фьючерсы Bybit и OKX. Одновременно поддерживается один активный журнал пары или отдельной позиции.
      Проверка подключения и баланс не подтверждают готовность к ордерам. Управление доступно через localhost или SSH-туннель.
    </n-alert>
    <n-alert v-if="!localAccess" type="error">Используйте локальный адрес или SSH-туннель.</n-alert>
    <n-card title="Разрешение демоордеров" size="small">
      <n-space vertical>
        <n-text>Ордера: {{ orderPermission ? 'разрешены' : 'отключены' }}; новые открытия: {{ openings ? 'разрешены' : 'отключены' }}.</n-text>
        <n-space>
          <n-button :disabled="busy || !localAccess || orderPermission" @click="action('permission', { enabled: true, confirmed: true }, 'Разрешить демоордера до перезапуска сервера?')">Разрешить демоордера</n-button>
          <n-button :disabled="busy || !localAccess || !openings" @click="action('stop-openings', { confirmed: true })">Остановить открытия</n-button>
          <n-button :disabled="busy || !localAccess || !orderPermission || openings" @click="action('resume-openings', { confirmed: true }, 'Снова разрешить открытие демопар?')">Возобновить открытия</n-button>
          <n-button :disabled="busy || !localAccess || !orderPermission" @click="action('permission', { enabled: false, confirmed: true }, 'Отключить все демоордера, включая закрытие?')">Отключить ордера</n-button>
        </n-space>
      </n-space>
    </n-card>
    <n-card title="Отдельная демопозиция" size="small">
      <n-space vertical>
        <n-form-item label="Биржа"><n-select v-model:value="singleVenue" :options="venues" /></n-form-item>
        <n-form-item label="Направление"><n-select v-model:value="singleSide" :options="[{ label: 'Long', value: 'buy' }, { label: 'Short', value: 'sell' }]" /></n-form-item>
        <n-text>Используются базовый актив и объём из формы пары.</n-text>
        <n-space>
          <n-button :disabled="busy || !localAccess || !openings" @click="action('single/open', { base: base.toUpperCase(), amount_usd: amount, venue: singleVenue, side: singleSide, confirmed: true }, 'Открыть отдельную фьючерсную демопозицию?')">Открыть позицию</n-button>
          <n-button :disabled="busy || !localAccess || !orderPermission" @click="action('single/close', { confirmed: true }, 'Закрыть записанную отдельную демопозицию reduce-only?')">Закрыть позицию</n-button>
        </n-space>
      </n-space>
    </n-card>
    <n-card title="Автоматическая демостратегия" size="small">
      <n-space vertical>
        <n-text>Стратегия: {{ strategyRunning ? 'работает' : 'остановлена' }}. После перезапуска требуется новое разрешение.</n-text>
        <n-form-item label="Объём на ногу USDT"><n-input v-model:value="strategy.trade_usd" /></n-form-item>
        <n-form-item label="Минимальный funding-спред, %"><n-input v-model:value="strategy.min_spread_pct" /></n-form-item>
        <n-form-item label="Минимальный спред после комиссий и проскальзывания, %"><n-input v-model:value="strategy.min_net_pct" /></n-form-item>
        <n-form-item label="Закрыть при funding-спреде не выше, %"><n-input v-model:value="strategy.exit_spread_pct" /></n-form-item>
        <n-form-item label="Максимальная разница цен, %"><n-input v-model:value="strategy.max_mark_spread_pct" /></n-form-item>
        <n-form-item label="Максимальное плечо"><n-input v-model:value="strategy.max_leverage" /></n-form-item>
        <n-form-item label="Максимальная комиссия на бирже, %"><n-input v-model:value="strategy.max_fee_pct" /></n-form-item>
        <n-form-item label="Максимальное проскальзывание на бирже, %"><n-input v-model:value="strategy.max_slippage_pct" /></n-form-item>
        <n-form-item label="Допустимая разница времени funding, сек"><n-input-number v-model:value="strategy.max_funding_gap_sec" :min="0" :max="604800" /></n-form-item>
        <n-form-item label="Интервал, сек"><n-input-number v-model:value="strategy.interval_sec" :min="30" :max="86400" /></n-form-item>
        <n-space>
          <n-button :disabled="busy || !localAccess || strategyRunning" @click="action('strategy/settings', { settings: strategy })">Сохранить настройки</n-button>
          <n-button :disabled="busy || !localAccess" @click="showStrategyEvents">События стратегии</n-button>
          <n-button :disabled="busy || !localAccess || !openings || strategyRunning" @click="action('strategy/start', { confirmed: true, settings: strategy }, 'Разрешить автоматическое открытие и закрытие демопары по указанным условиям?')">Запустить</n-button>
          <n-button :disabled="busy || !localAccess || !strategyRunning" @click="action('strategy/stop', { confirmed: true })">Остановить</n-button>
        </n-space>
        <pre v-if="strategyState" style="white-space: pre-wrap">{{ strategyState }}</pre>
      </n-space>
    </n-card>
    <n-card title="Демопара и позиции" size="small">
      <n-space vertical>
        <n-form-item label="Базовый актив (например BTC)"><n-input v-model:value="base" /></n-form-item>
        <n-form-item label="Биржа Long (Short — другая биржа)"><n-select v-model:value="longVenue" :options="venues" /></n-form-item>
        <n-form-item label="Объём на каждую ногу, USDT"><n-input v-model:value="amount" /></n-form-item>
        <n-space>
          <n-button :disabled="busy || !localAccess" @click="action('positions', { base: base.toUpperCase() })">Показать позиции и заявки</n-button>
          <n-button :disabled="busy || !localAccess" @click="action('history', { base: base.toUpperCase() })">Показать историю, комиссии и PnL</n-button>
          <n-button :disabled="busy || !localAccess" @click="showJournal">Показать журнал пары</n-button>
          <n-button :disabled="busy || !localAccess" @click="action('reconcile', { confirmed: true }, 'Сверить журнал с фактическими демозаявками и позициями?')">Сверить журнал</n-button>
          <n-button :disabled="busy || !localAccess || !orderPermission" @click="action('resolve-flat', { confirmed: true }, 'Подтвердить ручную сверку только если обе позиции закрыты и все заявки завершены?')">Завершить сверку пустой пары</n-button>
          <n-button :disabled="busy || !localAccess || !openings" @click="action('open', { base: base.toUpperCase(), amount_usd: amount, long_venue: longVenue, short_venue: longVenue === 'bybit' ? 'okx' : 'bybit', confirmed: true }, 'Открыть две рыночные фьючерсные ноги на демосчетах?')">Открыть пару</n-button>
          <n-button :disabled="busy || !localAccess || !orderPermission" @click="action('close', { confirmed: true }, 'Закрыть обе ноги записанной демопары reduce-only?')">Закрыть пару</n-button>
        </n-space>
      </n-space>
    </n-card>
    <n-card title="Отмена демозаявки" size="small">
      <n-space vertical>
        <n-select v-model:value="cancelVenue" :options="venues" />
        <n-input v-model:value="clientId" placeholder="clientOrderId из журнала" />
        <n-button :disabled="busy || !localAccess || !orderPermission" @click="action('cancel', { venue: cancelVenue, client_id: clientId, confirmed: true }, 'Отменить указанную заявку из журнала?')">Отменить заявку</n-button>
        <n-button :disabled="busy || !localAccess || !orderPermission" @click="action('cancel-open', { venue: cancelVenue, base: base.toUpperCase(), client_id: clientId, confirmed: true }, 'Отменить подтверждённую открытую демозаявку?')">Отменить открытую заявку</n-button>
      </n-space>
    </n-card>
    <n-alert v-if="status" type="info">{{ status }}</n-alert>
    <pre v-if="output" style="white-space: pre-wrap; overflow-wrap: anywhere">{{ output }}</pre>
  </n-space>
</template>
