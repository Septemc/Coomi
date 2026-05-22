import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

export const agentApi = {
  chat(message: string, sessionId?: string) {
    return api.post('/agent/chat', { message, session_id: sessionId })
  },
}

export const toolApi = {
  list() {
    return api.get('/tools')
  },
}

export default api
