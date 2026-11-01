<script setup lang="ts">
import { reactive, ref } from 'vue'
import { NAlert, NButton, NCard, NFormItem, NInput, NSpace, NText } from 'naive-ui'

type Venue = 'bybit' | 'okx' | 'bitget'
const venues: { id: Venue; name: string; passphrase: boolean }[] = [
  { id: 'bybit', name: 'Bybit Demo Trading', passphrase: false },
  { id: 'okx', name: 'OKX Demo Trading', passphrase: true },
  { id: 'bitget', name: 'Bitget Demo Trading', passphrase: true },
]
const forms = reactive<Record<Venue, { key: string; secret: string; passphrase: string }>>({
  bybit: { key: '', secret: '', passphrase: '' },
  okx: { key: '', secret: '', passphrase: '' },
  bitget: { key: '', secret: '', passphrase: '' },
})
const statuses = reactive<Record<Venue, string>>({ bybit: '', okx: '', bitget: '' })
const busy = ref<Venue | null>(null)
const localAccess = ['localhost', '127.0.0.1', '[::1]', '::1'].includes(window.location.hostname)

async function check(venue: Venue) {
  busy.value = venue
  statuses[venue] = ''
  try {
    const form = forms[venue]
    const response = await fetch('/api/demo/check', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ venue, environment: 'demo', api_key: form.key, api_secret: form.secret, passphrase: form.passphrase }),
    })
    if (!response.ok) throw new Error('failed')
    const data = await response.json()
    if (data.environment !== 'demo' || data.venue !== venue || data.connected !== true ||
        data.credentials !== '••••' || data.balance?.asset !== 'USDT' ||
        (data.balance.amount !== null && typeof data.balance.amount !== 'string')) throw new Error('invalid')
    statuses[venue] = data.balance.amount === null
      ? 'Демо-подключение подтверждено (ключ ••••); баланс USDT отсутствует в ответе.'
      : `Демо-подключение подтверждено (ключ ••••). Баланс USDT: ${data.balance.amount}`
  } catch {
    statuses[venue] = 'Проверка не пройдена. Проверьте демо-ключи и доступ к бирже.'
  } finally {
    forms[venue] = { key: '', secret: '', passphrase: '' }
    busy.value = null
  }
}
</script>

<template>
  <div class="demo-connections">
    <n-alert v-if="!localAccess" type="error">Проверка ключей доступна только через локальный адрес или SSH-туннель.</n-alert>
    <n-alert type="warning" title="Демо: только проверка подключения">
      Используйте только отдельные демо-ключи без права вывода. Ордера отключены; ключи не сохраняются и очищаются после проверки.
    </n-alert>
    <n-card v-for="venue in venues" :key="venue.id" :title="venue.name" size="small">
      <n-space vertical>
        <n-form-item label="Демо API key"><n-input v-model:value="forms[venue.id].key" type="password" autocomplete="off" /></n-form-item>
        <n-form-item label="Демо API secret"><n-input v-model:value="forms[venue.id].secret" type="password" autocomplete="off" /></n-form-item>
        <n-form-item v-if="venue.passphrase" label="Демо passphrase"><n-input v-model:value="forms[venue.id].passphrase" type="password" autocomplete="off" /></n-form-item>
        <n-button :loading="busy === venue.id" :disabled="busy !== null || !localAccess" @click="check(venue.id)">Проверить подключение</n-button>
        <n-text v-if="statuses[venue.id]">{{ statuses[venue.id] }}</n-text>
      </n-space>
    </n-card>
  </div>
</template>

<style scoped>
.demo-connections { display: grid; gap: 16px; max-width: 900px; }
</style>
