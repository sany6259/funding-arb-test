import { ref } from 'vue'

export type ExecutionMode = 'simulation' | 'demo' | 'unavailable'
export const executionMode = ref<ExecutionMode>('unavailable')
export const executionModeBusy = ref(false)
let generation = 0

export async function refreshExecutionMode(): Promise<void> {
  const currentGeneration = generation
  try {
    const response = await fetch('/api/execution-mode')
    if (!response.ok) throw new Error('unavailable')
    const data = await response.json()
    if (data.mode !== 'simulation' && data.mode !== 'demo') throw new Error('invalid')
    if (generation === currentGeneration) executionMode.value = data.mode
  } catch {
    if (generation === currentGeneration) executionMode.value = 'unavailable'
  }
}

export async function setExecutionMode(mode: 'simulation' | 'demo'): Promise<void> {
  generation++
  executionModeBusy.value = true
  try {
    const response = await fetch('/api/execution-mode', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode }),
    })
    if (!response.ok) throw new Error('unavailable')
    const data = await response.json()
    if (data.mode !== mode) throw new Error('invalid')
    executionMode.value = mode
  } catch {
    executionMode.value = 'unavailable'
  } finally {
    executionModeBusy.value = false
  }
}
