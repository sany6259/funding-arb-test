<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { NAlert, NButton, NCard, NFormItem, NInput, NSpace, NText } from 'naive-ui'

type Venue = 'bybit' | 'okx' | 'bitget'
const venues: { id: Venue; name: string; passphrase: boolean; asset: string; account: string }[] = [
  { id: 'bybit', name: 'Bybit Demo Trading', passphrase: false, asset: 'USD', account: 'Unified Account' },
  { id: 'okx', name: 'OKX Demo Trading', passphrase: true, asset: 'USDT', account: 'Trading Account' },
  { id: 'bitget', name: 'Bitget Demo Trading', passphrase: true, asset: 'USDT', account: 'Spot Account' },
]
const forms = reactive<Record<Venue, { key: string; secret: string; passphrase: string }>>({
  bybit: { key: '', secret: '', passphrase: '' }, okx: { key: '', secret: '', passphrase: '' }, bitget: { key: '', secret: '', passphrase: '' },
})
const configured = reactive<Record<Venue, boolean | null>>({ bybit: null, okx: null, bitget: null })
const statuses = reactive<Record<Venue, string>>({ bybit: 'Не проверено', okx: 'Не проверено', bitget: 'Не проверено' })
const busy = ref<Venue | null>(null)
const storeError = ref('')
const localAccess = ['localhost', '127.0.0.1', '[::1]', '::1'].includes(window.location.hostname)

async function refresh() {
  if (!localAccess) return
  try {
    const response = await fetch('/api/demo/credentials')
    if (!response.ok) throw new Error('store')
    const data = await response.json()
    for (const venue of venues) configured[venue.id] = data.configured?.[venue.id] === true
    storeError.value = ''
  } catch {
    for (const venue of venues) configured[venue.id] = null
    storeError.value = 'Защищённое хранилище недоступно.'
  }
}
onMounted(refresh)

async function action(venue: Venue, operation: 'save' | 'delete' | 'check') {
  busy.value = venue
  statuses[venue] = 'Не проверено'
  const paths = { save: '/api/demo/credentials', delete: '/api/demo/credentials/delete', check: '/api/demo/check' }
  const body: Record<string, string> = { venue, environment: 'demo' }
  if (operation === 'save') {
    const form = forms[venue]
    Object.assign(body, { api_key: form.key, api_secret: form.secret, passphrase: form.passphrase })
  }
  try {
    const response = await fetch(paths[operation], {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    if (!response.ok) throw new Error('failed')
    const data = await response.json()
    if (data.environment !== 'demo' || data.venue !== venue) throw new Error('invalid')
    if (operation === 'check') {
      const spec = venues.find(v => v.id === venue)!
      if (data.connected !== true || typeof data.orders_enabled !== 'boolean' || data.credentials !== '••••' ||
          data.balance?.asset !== spec.asset || data.balance.account_type !== spec.account ||
          (data.balance.amount !== null && typeof data.balance.amount !== 'string')) throw new Error('invalid')
      statuses[venue] = data.balance.amount === null
        ? `Подключение подтверждено; доступный баланс ${spec.account} отсутствует в ответе.`
        : `Подключение подтверждено. Доступно: ${data.balance.amount} ${spec.asset} (${spec.account}).`
    } else {
      configured[venue] = operation === 'save'
      statuses[venue] = operation === 'save' ? 'Ключ сохранён; подключение не проверено.' : 'Ключ удалён; подключение не проверено.'
      storeError.value = ''
    }
  } catch {
    statuses[venue] = operation === 'check'
      ? 'Проверка не пройдена: ошибка авторизации, баланса или связи с биржей.'
      : 'Операция не выполнена. Проверьте поля и защищённое хранилище.'
  } finally {
    forms[venue] = { key: '', secret: '', passphrase: '' }
    busy.value = null
  }
}
</script>

<template>
  <div class="demo-connections">
    <n-alert v-if="!localAccess" type="error">Доступ к ключам разрешён только через локальный адрес или SSH-туннель.</n-alert>
    <n-alert v-if="storeError" type="error">{{ storeError }}</n-alert>
    <n-alert type="warning" title="Демо-счета">
      Используйте отдельные демоключи без права вывода. Они сохраняются в защищённом хранилище. Проверка баланса не подтверждает готовность к торговле.
    </n-alert>
    <n-card v-for="venue in venues" :key="venue.id" :title="venue.name" size="small">
      <n-space vertical>
        <n-text>Среда: демо. {{ configured[venue.id] === null ? 'Состояние ключа неизвестно.' : (configured[venue.id] ? 'Ключ сохранён (••••).' : 'Сохранённого ключа нет.') }}</n-text>
        <n-form-item label="Демо API key"><n-input v-model:value="forms[venue.id].key" type="password" autocomplete="off" /></n-form-item>
        <n-form-item label="Демо API secret"><n-input v-model:value="forms[venue.id].secret" type="password" autocomplete="off" /></n-form-item>
        <n-form-item v-if="venue.passphrase" label="Демо passphrase"><n-input v-model:value="forms[venue.id].passphrase" type="password" autocomplete="off" /></n-form-item>
        <n-space>
          <n-button :disabled="busy !== null || !localAccess" @click="action(venue.id, 'save')">Сохранить / обновить</n-button>
          <n-button :disabled="busy !== null || !localAccess || !configured[venue.id]" @click="action(venue.id, 'check')">Проверить подключение</n-button>
          <n-button :disabled="busy !== null || !localAccess || !configured[venue.id]" @click="action(venue.id, 'delete')">Удалить ключ</n-button>
        </n-space>
        <n-text>{{ statuses[venue.id] }}</n-text>
      </n-space>
    </n-card>
  </div>
</template>

<style scoped>
.demo-connections { display: grid; gap: 16px; max-width: 900px; }
</style>
