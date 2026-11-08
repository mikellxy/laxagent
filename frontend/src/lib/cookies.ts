const memory = new Map<string, string>()
export const YEAR = 365 * 24 * 60 * 60
function persistent(name: string): string | null {
  try {
    const found = document.cookie.split('; ').find(item => item.startsWith(`${name}=`))
    if (found) return decodeURIComponent(found.slice(name.length + 1))
  } catch { /* Cookie access can be disabled by browser policy. */ }
  return null
}
export function readCookie(name: string): string | null { return memory.get(name) ?? persistent(name) }
export function writeCookie(name: string, value: string, seconds = YEAR) {
  try {
    document.cookie = `${name}=${encodeURIComponent(value)}; Max-Age=${seconds}; Path=/; SameSite=Lax${location.protocol === 'https:' ? '; Secure' : ''}`
    if (persistent(name) === value) { memory.delete(name); return }
  } catch { /* Keep this browser session usable without persistent cookies. */ }
  memory.set(name, value)
}
export function clearCookie(name: string) {
  memory.delete(name)
  try { document.cookie = `${name}=; Max-Age=0; Path=/; SameSite=Lax` } catch { /* No persistent storage. */ }
}
