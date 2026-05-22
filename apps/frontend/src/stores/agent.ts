import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { Message, Session } from '@/types'

export const useAgentStore = defineStore('agent', () => {
  const sessionId = ref<string | null>(null)
  const messages = ref<Message[]>([])
  const isLoading = ref(false)

  async function sendMessage(content: string) {
    isLoading.value = true
    try {
      // TODO: 调用API
      messages.value.push({ role: 'user', content })
    } finally {
      isLoading.value = false
    }
  }

  return {
    sessionId,
    messages,
    isLoading,
    sendMessage,
  }
})
