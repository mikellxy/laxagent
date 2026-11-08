export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}
export function errorText(payload: unknown): string {
  if (!payload || typeof payload !== 'object') return '请求失败'
  const detail = (payload as { detail?: unknown }).detail
  const value = detail && typeof detail === 'object' && 'error' in detail ? detail.error : detail
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map(item => typeof item?.msg === 'string' ? item.msg : '').filter(Boolean).join('；') || '请求参数无效'
  return '请求失败'
}
export async function responseError(response: Response): Promise<ApiError> {
  let message = `请求失败 (${response.status})`
  try { message = errorText(await response.json()) } catch { /* Non-JSON gateway failure. */ }
  return new ApiError(response.status, message)
}
export function messageOf(error: unknown): string { return error instanceof Error ? error.message : '请求失败' }
