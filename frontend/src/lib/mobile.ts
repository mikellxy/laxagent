import { useEffect, useSyncExternalStore } from 'react'

// Keep this query aligned with the mobile layout rules in the stylesheets.
export const mobileQuery = '(max-width: 700px), (max-width: 1000px) and (pointer: coarse)'
const subscribe = (notify: () => void) => {
  const media = window.matchMedia?.(mobileQuery)
  media?.addEventListener('change', notify)
  return () => media?.removeEventListener('change', notify)
}
export function useMobileLayout() {
  return useSyncExternalStore(subscribe, () => window.matchMedia?.(mobileQuery).matches ?? false, () => false)
}

export function useMobileViewport() {
  useEffect(() => {
    const viewport = window.visualViewport
    if (!viewport) return
    const update = () => {
      // Preserve browser pinch zoom instead of resizing the application beneath it.
      if (Math.abs(viewport.scale - 1) > 0.01) return
      document.documentElement.style.setProperty('--viewport-height', `${viewport.height}px`)
      document.documentElement.style.setProperty('--viewport-top', `${viewport.offsetTop}px`)
    }
    update()
    viewport.addEventListener('resize', update)
    viewport.addEventListener('scroll', update)
    return () => {
      viewport.removeEventListener('resize', update)
      viewport.removeEventListener('scroll', update)
      document.documentElement.style.removeProperty('--viewport-height')
      document.documentElement.style.removeProperty('--viewport-top')
    }
  }, [])
}
