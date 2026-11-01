export type SupportedLocale = 'ru' | 'en' | 'zh-CN' | 'zh-TW'

export const SUPPORTED_LOCALES: { value: SupportedLocale; label: string }[] = [
  { value: 'ru', label: 'Русский' },
  { value: 'en', label: 'English' },
  { value: 'zh-CN', label: '简体中文' },
  { value: 'zh-TW', label: '繁體中文' },
]

export function normalizeLocale(value: string | null): SupportedLocale {
  return SUPPORTED_LOCALES.some((entry) => entry.value === value)
    ? value as SupportedLocale
    : 'ru'
}

export function initialLocale(storage?: Pick<Storage, 'getItem' | 'setItem'>): SupportedLocale {
  try {
    const saved = storage?.getItem('locale') ?? null
    const locale = normalizeLocale(saved)
    if (saved !== null && saved !== locale) storage?.setItem('locale', locale)
    return locale
  } catch {
    return 'ru'
  }
}
