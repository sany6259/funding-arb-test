<script setup lang="ts">
import { onMounted } from 'vue'
import { NCard, NText, NIcon, NDivider, NSpin } from 'naive-ui'
import { CheckmarkCircleOutline, CloseCircleOutline } from '@vicons/ionicons5'
import { getCredentialsStatus } from '@/composables/useApi'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()
const credentials = getCredentialsStatus()

const backendDescriptions: Record<string, string> = {
  "macOS Keychain / System Key Management": "mac",
  "keyring package not installed": "keyringMissing",
  "systemd-creds (Linux TPM2)": "systemd",
  "Linux only": "linuxOnly",
  "age encrypted file": "age",
  "age tool not installed": "ageMissing",
  "Plaintext JSON (fallback)": "plaintext"
}
function localizedDescription(description: string): string {
  const key = backendDescriptions[description]
  return key ? t(`credentialBackends.${key}`) : description
}

onMounted(async () => {
  await credentials.refresh()
})
</script>

<template>
  <div class="settings-page">
    <n-card :title="t('settings.credentialBackends')" size="small">
      <n-spin :show="credentials.loading.value">
        <div class="backend-list">
          <div v-for="(info, name) in credentials.data.value?.backends" :key="name" class="backend-item">
            <div class="backend-left">
              <n-icon v-if="info.available" color="#18a058" size="18"><CheckmarkCircleOutline /></n-icon>
              <n-icon v-else color="#d03050" size="18"><CloseCircleOutline /></n-icon>
              <n-text>{{ name }}</n-text>
            </div>
            <n-text depth="3" style="font-size: 11px">{{ localizedDescription(info.description) }}</n-text>
          </div>
        </div>
        <n-divider style="margin: 12px 0" />
        <div class="backend-summary">
          <n-text depth="3" style="font-size: 12px">{{ t('settings.configuredVenues') }}: {{ credentials.data.value?.venues_configured?.join(', ') || t('settings.none') }}</n-text>
        </div>
        <div class="backend-summary" style="margin-top: 4px">
          <n-text depth="3" style="font-size: 12px">{{ t('settings.missingVenues') }}: {{ credentials.data.value?.venues_missing?.join(', ') || t('settings.none') }}</n-text>
        </div>
      </n-spin>
    </n-card>
  </div>
</template>

<style scoped>
.settings-page {
  height: 100%;
  max-width: 900px;
  margin: 0 auto;
}
.backend-list { display: flex; flex-direction: column; gap: 10px; }
.backend-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 12px;
  background: rgba(255, 255, 255, 0.03);
  border-radius: 6px;
}
.backend-left { display: flex; align-items: center; gap: 8px; }
.backend-summary { font-weight: 500; }

@media (max-width: 700px) {
  .backend-item {
    align-items: flex-start;
    flex-direction: column;
    gap: 6px;
  }
}
</style>
