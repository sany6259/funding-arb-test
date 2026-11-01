import { createI18n } from 'vue-i18n'
import en from './locales/en.json'
import ru from './locales/ru.json'
import zhCN from './locales/zh-CN.json'
import zhTW from './locales/zh-TW.json'
import { initialLocale, type SupportedLocale } from './locale'

export { SUPPORTED_LOCALES, type SupportedLocale } from './locale'

function browserStorage(): Storage | undefined {
  try {
    return typeof localStorage === 'undefined' ? undefined : localStorage
  } catch {
    return undefined
  }
}

const i18n = createI18n({
  legacy: false,
  locale: initialLocale(browserStorage()),
  fallbackLocale: 'en',
  messages: { ru, en, 'zh-CN': zhCN, 'zh-TW': zhTW },
})

export default i18n
export type MessageSchema = typeof en

export function setLocale(locale: SupportedLocale) {
  i18n.global.locale.value = locale
  try {
    browserStorage()?.setItem('locale', locale)
  } catch {
    // The selected language remains active when storage is unavailable.
  }
}
