import { HttpErrorResponse } from '@angular/common/http';
import { ApiErrorBody } from './models/api.model';

/** 把任意 HTTP 错误收敛成统一错误体（prd-07 §1.2），非标准响应返回 null。 */
export function readApiError(error: unknown): ApiErrorBody | null {
  if (!(error instanceof HttpErrorResponse)) {
    return null;
  }
  const body: unknown = error.error;
  if (typeof body !== 'object' || body === null) {
    return null;
  }
  const candidate = body as Partial<ApiErrorBody>;
  if (typeof candidate.code !== 'string' || typeof candidate.message !== 'string') {
    return null;
  }
  return candidate as ApiErrorBody;
}

/** 错误提示优先级：后端 message → 网络异常 → 兜底文案。 */
export function readErrorMessage(error: unknown, fallback = '操作失败，请稍后重试'): string {
  if (error instanceof HttpErrorResponse && error.status === 0) {
    return '网络异常，请检查网络连接后重试';
  }
  return readApiError(error)?.message || fallback;
}
