export interface SlashCommand {
  command: string
  description: string
  args?: Array<Record<string, unknown>>
}

export interface MemorySystemConfig {
  type: 'file' | 'database' | 'api'
  auto_save: boolean
  user_controllable: boolean
}

export interface ToolConfig {
  name: string
  description: string
}

export interface SettingField {
  key: string
  label: string
  type: 'text' | 'dropdown' | 'checkbox' | 'number'
  options?: string[]
}

export interface ProviderCapabilities {
  // Basic features
  supports_streaming: boolean
  supports_file_upload: boolean
  supports_image_input: boolean

  // Advanced features
  slash_commands: SlashCommand[] | null
  memory_system: MemorySystemConfig | null
  tools: ToolConfig[] | null

  // UI display settings
  show_session_list: boolean
  show_model_selector: boolean
  custom_settings: SettingField[] | null
}
