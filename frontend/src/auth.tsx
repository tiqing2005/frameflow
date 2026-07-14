/* oxlint-disable react/only-export-components -- provider and hook form one authentication boundary */
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { api, ApiError, setCsrfToken } from './api'
import type { AuthSessionInfo } from './types'

interface AuthContextValue {
  session: AuthSessionInfo | null
  loading: boolean
  error: string | null
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)
const disabledSession: AuthSessionInfo = {
  auth_enabled: false,
  configured: false,
  authenticated: true,
  user: null,
  csrf_token: null,
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<AuthSessionInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const applySession = useCallback((next: AuthSessionInfo) => {
    setCsrfToken(next.csrf_token)
    setSession(next)
    setError(null)
  }, [])

  const refresh = useCallback(async () => {
    try {
      applySession(await api.authSession())
    } catch (reason) {
      // Compatibility with an older/mock API that predates application auth.
      if (reason instanceof ApiError && reason.status === 404) applySession(disabledSession)
      else {
        setSession(null)
        setError(reason instanceof Error ? reason.message : '无法确认登录状态')
      }
    } finally {
      setLoading(false)
    }
  }, [applySession])

  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => {
    const onRequired = () => {
      setCsrfToken(null)
      setSession((current) => current ? { ...current, authenticated: false, user: null, csrf_token: null } : current)
    }
    window.addEventListener('frameflow:auth-required', onRequired)
    return () => window.removeEventListener('frameflow:auth-required', onRequired)
  }, [])

  const login = useCallback(async (username: string, password: string) => {
    applySession(await api.login(username, password))
  }, [applySession])

  const logout = useCallback(async () => {
    try {
      await api.logout()
    } finally {
      setCsrfToken(null)
      setSession((current) => current ? { ...current, authenticated: false, user: null, csrf_token: null } : current)
    }
  }, [])

  return (
    <AuthContext.Provider value={{ session, loading, error, login, logout, refresh }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth 必须在 AuthProvider 中使用')
  return value
}
