// 消息类型
export interface Message {
  role: 'user' | 'assistant' | 'system'
  content: string
}

// 工具调用类型
export interface ToolCall {
  name: string
  args: Record<string, unknown>
}

// 会话类型
export interface Session {
  id: string
  messages: Message[]
}
