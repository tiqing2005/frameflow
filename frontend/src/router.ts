import { createElement, useEffect, useState, type AnchorHTMLAttributes, type MouseEvent } from 'react'

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

export function navigate(path: string, options?: { replace?: boolean }) {
  window.history[options?.replace ? 'replaceState' : 'pushState']({}, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

export function useRoute() {
  const [route, setRoute] = useState(parseLocation)
  useEffect(() => {
    const onChange = () => setRoute(parseLocation())
    window.addEventListener('popstate', onChange)
    return () => window.removeEventListener('popstate', onChange)
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
        navigate(href)
      },
    },
    children,
  )
}
