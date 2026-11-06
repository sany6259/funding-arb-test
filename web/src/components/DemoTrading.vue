<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { NAlert, NButton, NCard, NFormItem, NInput, NSelect, NSpace, NText } from 'naive-ui'

const localAccess = ['localhost', '127.0.0.1', '[::1]', '::1'].includes(window.location.hostname)
const base = ref('BTC')
const amount = ref('100')
const longVenue = ref('bybit')
const cancelVenue = ref('bybit')
const clientId = ref('')
const orderPermission = ref(false)
const openings = ref(false)
const status = ref('')
const output = ref('')
const busy = ref(false)
const venues = [{ label: 'Bybit Demo', value: 'bybit' }, { label: 'OKX Demo', value: 'okx' }]

async function refreshPermission() {
  try {
    const response = await fetch('/api/execution-mode')
    const data = await response.json()
    if (!response.ok || data.mode !== 'demo' || typeof data.orders_enabled !== 'boolean' || typeof data.openings_enabled !== 'boolean') throw new Error()
    orderPermission.value = data.orders_enabled
    openings.value = data.openings_enabled
  } catch {
    orderPermission.value = false
    openings.value = false
    status.value = 'Состояние режима недоступно.'
  }
}
let permissionTimer: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  refreshPermission()
  permissionTimer = setInterval(refreshPermission, 5000)
})
onUnmounted(() => { if (permissionTimer) clearInterval(permissionTimer) })

async function showJournal() {
  if (!localAccess || busy.value) return
  busy.value = true
  output.value = ''
  try {
    const response = await fetch('/api/demo/trading/journal')
    const data = await response.json()
    if (!response.ok) throw new Error(data.error || 'Журнал недоступен.')
    output.value = JSON.stringify(data.result, null, 2)
    status.value = data.result === null ? 'Журнал демопары отсутствует.' : 'Данные журнала показаны ниже; это не подтверждение текущего состояния биржи.'
  } catch (error) {
    status.value = error instanceof Error ? error.message : 'Журнал недоступен.'
  } finally {
    busy.value = false
  }
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
      Торгуются только две фьючерсные ноги. Автоматическая стратегия и отдельные ордера в этой панели пока недоступны.
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
    <n-card title="Демопара и позиции" size="small">
      <n-space vertical>
        <n-form-item label="Базовый актив (например BTC)"><n-input v-model:value="base" /></n-form-item>
        <n-form-item label="Биржа Long (Short — другая биржа)"><n-select v-model:value="longVenue" :options="venues" /></n-form-item>
        <n-form-item label="Объём на каждую ногу, USDT"><n-input v-model:value="amount" /></n-form-item>
        <n-space>
          <n-button :disabled="busy || !localAccess" @click="action('positions', { base: base.toUpperCase() })">Показать позиции и заявки</n-button>
          <n-button :disabled="busy || !localAccess" @click="showJournal">Показать журнал пары</n-button>
          <n-button :disabled="busy || !localAccess || !openings" @click="action('open', { base: base.toUpperCase(), amount_usd: amount, long_venue: longVenue, short_venue: longVenue === 'bybit' ? 'okx' : 'bybit', confirmed: true }, 'Открыть две рыночные фьючерсные ноги на демосчетах?')">Открыть пару</n-button>
          <n-button :disabled="busy || !localAccess || !orderPermission" @click="action('close', { confirmed: true }, 'Закрыть обе ноги записанной демопары reduce-only?')">Закрыть пару</n-button>
        </n-space>
      </n-space>
    </n-card>
    <n-card title="Отмена неопределённой заявки из журнала" size="small">
      <n-space vertical>
        <n-select v-model:value="cancelVenue" :options="venues" />
        <n-input v-model:value="clientId" placeholder="clientOrderId из журнала" />
        <n-button :disabled="busy || !localAccess || !orderPermission" @click="action('cancel', { venue: cancelVenue, client_id: clientId, confirmed: true }, 'Отменить указанную заявку из журнала?')">Отменить заявку</n-button>
      </n-space>
    </n-card>
    <n-alert v-if="status" type="info">{{ status }}</n-alert>
    <pre v-if="output" style="white-space: pre-wrap; overflow-wrap: anywhere">{{ output }}</pre>
  </n-space>
</template>
