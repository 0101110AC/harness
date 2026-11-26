import { HttpContextToken } from '@angular/common/http';

/** 与后端 `API_V1_PREFIX` 保持一致；生产环境由 Nginx / 网关同域转发。 */
export const API_BASE_URL = '/api/v1';

/** 标记该请求已经过一次静默续期，避免 401 → refresh → 401 的死循环。 */
export const RETRIED = new HttpContextToken<boolean>(() => false);
