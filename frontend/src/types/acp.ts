export type SessionProvider = 'claude_code' | 'kiro' | 'openclaw' | 'bedrock'

export type MessageRole = 'user' | 'assistant' | 'system' | 'tool_use' | 'tool_result'

export interface ACPSession {
  session_id: string
  provider: SessionProvider
  cwd: string
  first_message: string
  updated_at: string
}

export interface ACPMessage {
  role: MessageRole
  content: string
  timestamp: string
  tool_name?: string
  tool_output?: string
  tool_input?: string | Record<string, unknown>
  tool_result?: string | Record<string, unknown>
}

export interface ACPSessionDetail {
  session_id: string
  provider: SessionProvider
  cwd: string
  messages: ACPMessage[]
  created_at: string
  updated_at: string
}

export interface JSONRPCRequest {
  jsonrpc: '2.0'
  method: string
  params?: Record<string, unknown>
  id: number
}

export interface JSONRPCResponse<T = unknown> {
  jsonrpc: '2.0'
  result?: T
  error?: {
    code: number
    message: string
    data?: unknown
  }
  id: number
}
