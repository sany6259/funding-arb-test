<script setup lang="ts">
import { ref, h, computed, onMounted, onUnmounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  NConfigProvider,
  NLayout,
  NLayoutSider,
  NLayoutHeader,
  NLayoutContent,
  NMenu,
  NIcon,
  NBadge,
  NText,
  NMessageProvider,
  NSelect,
  darkTheme, ruRU, enUS, zhCN, zhTW, dateRuRU, dateEnUS, dateZhCN, dateZhTW,
  type MenuOption,
} from 'naive-ui'
import {
  SearchOutline,
  BriefcaseOutline,
  StatsChartOutline,
  DocumentTextOutline,
  PulseOutline,
  KeyOutline,
  WalletOutline,
  ToggleOutline,
  CardOutline,
  LockClosedOutline,
} from '@vicons/ionicons5'
import { useWebSocket } from '@/composables/useApi'
import { isDemoMode, useDemoSnapshot } from '@/composables/useDemoSnapshot'
import DemoBanner from '@/components/DemoBanner.vue'
import DemoConnections from '@/components/DemoConnections.vue'
import { executionMode, executionModeBusy, refreshExecutionMode, setExecutionMode } from '@/composables/useExecutionMode'
import i18n, { setLocale, SUPPORTED_LOCALES, type SupportedLocale } from '@/i18n'

const { t } = useI18n()
const naiveLocale = computed(() => ({ ru: ruRU, en: enUS, 'zh-CN': zhCN, 'zh-TW': zhTW }[currentLocale.value]))
const naiveDateLocale = computed(() => ({ ru: dateRuRU, en: dateEnUS, 'zh-CN': dateZhCN, 'zh-TW': dateZhTW }[currentLocale.value]))
const router = useRouter()
const route = useRoute()
const collapsed = ref(true)

const activeKey = computed(() =>
  route.path.startsWith('/docs') ? '/docs' : route.path,
)

const menuOptions = computed<MenuOption[]>(() => [
  {
    label: t('menu.scanner'),
    key: '/',
    icon: () => h(NIcon, null, { default: () => h(SearchOutline) }),
  },
  {
    label: t('menu.positions'),
    key: '/positions',
    icon: () => h(NIcon, null, { default: () => h(BriefcaseOutline) }),
  },
  {
    label: t('menu.backtest'),
    key: '/backtest',
    icon: () => h(NIcon, null, { default: () => h(StatsChartOutline) }),
  },
  {
    label: t('menu.cex'),
    key: '/cex',
    icon: () => h(NIcon, null, { default: () => h(KeyOutline) }),
  },
  {
    label: t('menu.dex'),
    key: '/dex',
    icon: () => h(NIcon, null, { default: () => h(WalletOutline) }),
  },
  {
    label: t('menu.strategy'),
    key: '/strategy',
    icon: () => h(NIcon, null, { default: () => h(ToggleOutline) }),
  },
  {
    label: t('menu.fees'),
    key: '/fees',
    icon: () => h(NIcon, null, { default: () => h(CardOutline) }),
  },
  {
    label: t('menu.advanced'),
    key: '/advanced',
    icon: () => h(NIcon, null, { default: () => h(LockClosedOutline) }),
  },
  {
    label: t('menu.docs'),
    key: '/docs',
    icon: () => h(NIcon, null, { default: () => h(DocumentTextOutline) }),
  },
])

const currentLocale = computed({
  get: () => i18n.global.locale.value as SupportedLocale,
  set: (val: SupportedLocale) => setLocale(val),
})

const { connected, connect: wsConnect, disconnect: wsDisconnect } = useWebSocket()

// Demo-mode snapshot state (drives the banner + suppresses backend calls).
// Unwrap refs here so the template can use plain values (Vue auto-unwraps
// top-level refs in templates, but not nested refs inside a plain object).
const _demoApi = isDemoMode ? useDemoSnapshot() : null
const demoUrl = _demoApi?.url ?? ''
const demoSnapshot = _demoApi?.snapshot ?? null
const demoLoading = _demoApi?.loading ?? null
const demoScanTime = computed(() => demoSnapshot?.value?.meta.scan_timestamp ?? null)
let modeTimer: ReturnType<typeof setInterval> | undefined

function handleMenuUpdate(key: string) {
  router.push(key)
}

onMounted(() => {
  // Skip the WebSocket / trading-mode polls in demo mode — there is no
  // backend to talk to, and the static host would 404 on /ws and /api/*.
  if (!isDemoMode) {
    refreshExecutionMode()
    modeTimer = setInterval(() => {
      if (!executionModeBusy.value) void refreshExecutionMode()
    }, 5000)
    wsConnect()
  } else {
    _demoApi?.ensure()
  }
})

onUnmounted(() => {
  if (modeTimer) clearInterval(modeTimer)
  wsDisconnect()
})
</script>

<template>
  <n-config-provider :theme="darkTheme" :locale="naiveLocale" :date-locale="naiveDateLocale">
    <n-layout has-sider class="app-layout">
      <n-layout-sider
        bordered
        collapse-mode="width"
        :collapsed-width="64"
        :width="200"
        :collapsed="collapsed"
        show-trigger
        @collapse="collapsed = true"
        @expand="collapsed = false"
        :native-scrollbar="false"
        content-class="sider-content"
        class="app-sider"
      >
        <div class="sider-logo">
          <n-icon size="28" color="#18a058">
            <PulseOutline />
          </n-icon>
          <transition name="fade">
            <span v-if="!collapsed" class="logo-text">Funding Arb</span>
          </transition>
        </div>
        <n-menu
          :collapsed="collapsed"
          :collapsed-width="64"
          :collapsed-icon-size="22"
          :options="menuOptions"
          :value="activeKey"
          @update:value="handleMenuUpdate"
        />
      </n-layout-sider>

      <n-layout>
        <n-layout-header bordered class="app-header">
          <div class="header-left">
            <n-text class="header-title">{{ t('app.title') }}</n-text>
          </div>
          <div class="header-right">
            <n-select
              :value="executionMode"
              :disabled="isDemoMode || executionModeBusy || executionMode === 'unavailable'"
              :options="[
                { label: 'Симуляция', value: 'simulation' },
                { label: 'Демо (только проверка)', value: 'demo' },
                { label: 'Реальная торговля (недоступна)', value: 'live', disabled: true },
                { label: 'Режим недоступен', value: 'unavailable', disabled: true },
              ]"
              size="tiny" style="width: 220px"
              @update:value="(v: 'simulation' | 'demo') => setExecutionMode(v)"
            />
            <n-select
              v-model:value="currentLocale"
              :options="SUPPORTED_LOCALES"
              size="tiny"
              style="width: 120px"
            />
            <n-badge :type="isDemoMode ? 'success' : (connected ? 'success' : 'error')" :dot="true" />
            <n-text depth="3" class="header-status">
              {{ isDemoMode ? t('app.demoLive') : (connected ? t('app.connected') : t('app.disconnected')) }}
            </n-text>
          </div>
        </n-layout-header>

        <n-layout-content class="app-content" :native-scrollbar="false">
          <DemoBanner
            v-if="isDemoMode"
            :source="demoUrl"
            :scan-time="demoScanTime"
            :loading="demoLoading ?? false"
          />
          <n-message-provider>
            <DemoConnections v-if="!isDemoMode && executionMode === 'demo'" />
            <n-text v-else-if="!isDemoMode && executionMode === 'unavailable'" type="error">Не удалось проверить режим исполнения. Торговля недоступна.</n-text>
            <n-text v-else-if="route.path === '/dex' || route.path === '/cex'" type="warning">Настройка торговых ключей и кошельков отключена. Для проверки демо-ключей выберите режим «Демо».</n-text>
            <router-view v-else />
          </n-message-provider>
        </n-layout-content>
      </n-layout>
    </n-layout>
  </n-config-provider>
</template>

<style scoped>
.app-layout {
  height: 100vh;
  width: 100vw;
  box-sizing: border-box;
}

.app-sider {
  background-color: #101014 !important;
}

.sider-content {
  display: flex;
  flex-direction: column;
}

.sider-logo {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  height: 56px;
  padding: 0 16px;
  border-bottom: 1px solid #2d2d30;
}

.logo-text {
  font-size: 16px;
  font-weight: 700;
  color: #18a058;
  white-space: nowrap;
  letter-spacing: 0.5px;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

.app-header {
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  background-color: #18181c;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.header-title {
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 0.5px;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.header-status {
  font-size: 13px;
}

.app-content {
  padding: 20px;
  background-color: #18181c;
  height: calc(100vh - 56px);
  box-sizing: border-box;
  overflow: auto;
}
</style>
