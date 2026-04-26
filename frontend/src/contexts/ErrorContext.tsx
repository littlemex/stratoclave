/**
 * Global Error Context
 *
 * Provides centralized error state management for the application.
 * Supports error queuing, auto-dismiss, and typed error messages.
 */

import {
  createContext,
  useContext,
  useReducer,
  useCallback,
  useEffect,
  useRef,
  ReactNode,
} from 'react'

export type ErrorType = 'server' | 'rate_limit' | 'unauthorized' | 'forbidden' | 'timeout' | 'network' | 'parse' | 'generic'

export interface AppError {
  id: string
  message: string
  type: ErrorType
  timestamp: number
  dismissed: boolean
}

interface ErrorState {
  errors: AppError[]
}

type ErrorAction =
  | { type: 'ADD_ERROR'; error: AppError }
  | { type: 'DISMISS_ERROR'; id: string }
  | { type: 'CLEAR_ALL' }

interface ErrorContextValue {
  errors: AppError[]
  showError: (message: string, type?: ErrorType) => void
  dismissError: (id: string) => void
  clearAll: () => void
}

const ErrorContext = createContext<ErrorContextValue | null>(null)

const AUTO_DISMISS_MS = 5000

let errorIdCounter = 0

function errorReducer(state: ErrorState, action: ErrorAction): ErrorState {
  switch (action.type) {
    case 'ADD_ERROR':
      return {
        errors: [...state.errors, action.error],
      }
    case 'DISMISS_ERROR':
      return {
        errors: state.errors.map((e) =>
          e.id === action.id ? { ...e, dismissed: true } : e
        ),
      }
    case 'CLEAR_ALL':
      return { errors: [] }
    default:
      return state
  }
}

interface ErrorProviderProps {
  children: ReactNode
}

export function ErrorProvider({ children }: ErrorProviderProps) {
  const [state, dispatch] = useReducer(errorReducer, { errors: [] })
  const timersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())

  const dismissError = useCallback((id: string) => {
    dispatch({ type: 'DISMISS_ERROR', id })
    const timer = timersRef.current.get(id)
    if (timer) {
      clearTimeout(timer)
      timersRef.current.delete(id)
    }
  }, [])

  const showError = useCallback(
    (message: string, type: ErrorType = 'generic') => {
      const id = `error-${++errorIdCounter}-${Date.now()}`
      const error: AppError = {
        id,
        message,
        type,
        timestamp: Date.now(),
        dismissed: false,
      }

      dispatch({ type: 'ADD_ERROR', error })

      // Auto-dismiss after 5 seconds
      const timer = setTimeout(() => {
        dispatch({ type: 'DISMISS_ERROR', id })
        timersRef.current.delete(id)
      }, AUTO_DISMISS_MS)

      timersRef.current.set(id, timer)
    },
    []
  )

  const clearAll = useCallback(() => {
    // Clear all timers
    timersRef.current.forEach((timer) => clearTimeout(timer))
    timersRef.current.clear()
    dispatch({ type: 'CLEAR_ALL' })
  }, [])

  // Cleanup timers on unmount
  useEffect(() => {
    return () => {
      timersRef.current.forEach((timer) => clearTimeout(timer))
    }
  }, [])

  // Clean up dismissed errors periodically (remove from state after animation)
  useEffect(() => {
    const interval = setInterval(() => {
      const now = Date.now()
      const activeErrors = state.errors.filter(
        (e) => !e.dismissed || now - e.timestamp < AUTO_DISMISS_MS + 1000
      )
      if (activeErrors.length < state.errors.length) {
        dispatch({ type: 'CLEAR_ALL' })
        activeErrors
          .filter((e) => !e.dismissed)
          .forEach((e) => dispatch({ type: 'ADD_ERROR', error: e }))
      }
    }, 10000)

    return () => clearInterval(interval)
  }, [state.errors])

  const visibleErrors = state.errors.filter((e) => !e.dismissed)

  return (
    <ErrorContext.Provider value={{ errors: visibleErrors, showError, dismissError, clearAll }}>
      {children}
    </ErrorContext.Provider>
  )
}

/**
 * Hook to use the error context
 */
export function useError() {
  const context = useContext(ErrorContext)
  if (!context) {
    throw new Error('useError must be used within an ErrorProvider')
  }
  return context
}

/**
 * Get the error message for a given HTTP status code
 */
export function getErrorMessageForStatus(status: number, detail?: string): { message: string; type: ErrorType } {
  switch (status) {
    case 401:
      return { message: 'Unauthorized', type: 'unauthorized' }
    case 403:
      return { message: detail || 'Permission denied', type: 'forbidden' }
    case 429:
      return { message: 'Too many requests. Please wait.', type: 'rate_limit' }
    case 500:
      return { message: detail || 'Server error occurred. Please try again.', type: 'server' }
    case 502:
    case 503:
    case 504:
      return { message: 'Server error occurred. Please try again.', type: 'server' }
    default:
      if (status >= 400 && status < 500) {
        return { message: detail || `Request failed (${status})`, type: 'generic' }
      }
      if (status >= 500) {
        return { message: detail || 'Server error occurred. Please try again.', type: 'server' }
      }
      return { message: detail || 'An unexpected error occurred', type: 'generic' }
  }
}
