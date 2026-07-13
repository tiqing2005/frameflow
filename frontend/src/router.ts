import { createElement, useEffect, useState, type AnchorHTMLAttributes, type MouseEvent } from 'react'

type NavigationGuard = (path: string) => boolean | Promise<boolean>

const navigationGuards = new Set<NavigationGuard>()
let navigationInFlight: Promise<boolean> | null = null
const routeChangeEvent = 'frameflow:route-change'

const currentPath = () => `${window.location.pathname}${window.location.search}${window.location.hash}`

export type Route =
  | { name: 'dashboard' }
  | { name: 'new' }
  | { name: 'processing'; projectId: string; jobId?: string }
  | { name: 'project'; projectId: string }
  | { name: 'assets' }
  | { name: 'runs' }
  | { name: 'demo' }
  | { name: 'not-found' }

function parseLocation(): Route {
  const path = window.location.pathname.replace(/\/+$/, '') || '/'
  if (path === '/' || path === '/projects') return { name: 'dashboard' }
  if (path === '/projects/new') return { name: 'new' }
  if (path === '/assets') return { name: 'assets' }
  if (path === '/runs') return { name: 'runs' }
  if (path === '/demo') return { name: 'demo' }
  const processing = path.match(/^\/projects\/([^/]+)\/processing$/)
  if (processing) return {
    name: 'processing',
    projectId: decodeURIComponent(processing[1]),
    jobId: new URLSearchParams(window.location.search).get('job') || undefined,
  }
  const project = path.match(/^\/projects\/([^/]+)$/)
  if (project) return { name: 'project', projectId: decodeURIComponent(project[1]) }
  return { name: 'not-found' }
}

async function canNavigate(path: string) {
  for (const guard of navigationGuards) {
    if (!await guard(path)) return false
  }
  return true
}

export function addNavigationGuard(guard: NavigationGuard) {
  navigationGuards.add(guard)
  return () => {
    navigationGuards.delete(guard)
  }
}

export function navigate(path: string, options?: { replace?: boolean }): Promise<boolean> {
  if (navigationInFlight) return navigationInFlight
  const pending = (async () => {
    if (!await canNavigate(path)) return false
    window.history[options?.replace ? 'replaceState' : 'pushState']({}, '', path)
    window.dispatchEvent(new Event(routeChangeEvent))
    return true
  })()
  navigationInFlight = pending
  void pending.finally(() => {
    if (navigationInFlight === pending) navigationInFlight = null
  })
  return pending
}

export function useRoute() {
  const [route, setRoute] = useState(parseLocation)
  useEffect(() => {
    let activePath = currentPath()
    const onChange = () => {
      activePath = currentPath()
      setRoute(parseLocation())
    }
    const onPopState = () => {
      const targetPath = currentPath()
      if (targetPath === activePath) return
      if (navigationInFlight) {
        window.history.pushState({}, '', activePath)
        return
      }
      const pending = (async () => {
        if (!await canNavigate(targetPath)) {
          window.history.pushState({}, '', activePath)
          return false
        }
        activePath = targetPath
        setRoute(parseLocation())
        return true
      })()
      navigationInFlight = pending
      void pending.finally(() => {
        if (navigationInFlight === pending) navigationInFlight = null
      })
    }
    window.addEventListener(routeChangeEvent, onChange)
    window.addEventListener('popstate', onPopState)
    return () => {
      window.removeEventListener(routeChangeEvent, onChange)
      window.removeEventListener('popstate', onPopState)
    }
  }, [])
  return route
}

export function AppLink({ href, children, onClick, ...props }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) {
  return createElement(
    'a',
    {
      ...props,
      href,
      onClick: (event: MouseEvent<HTMLAnchorElement>) => {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
        event.preventDefault()
        onClick?.(event)
        void navigate(href)
      },
    },
    children,
  )
}
