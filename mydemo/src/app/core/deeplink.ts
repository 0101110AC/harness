/**
 * deeplink 安全校验（prd-04 §7）。
 *
 * 站内信的 `deeplink` 是服务端下发的字符串，但它会直接交给 Router，
 * 等于把「跳到哪儿」的决定权交给了消息体——消息体一旦被污染就是开放重定向。
 * 这里只放行站内绝对路径：
 * - 必须以单个 `/` 开头（`//evil.com` 是协议相对 URL，浏览器会当跨站）；
 * - 不含 `://`（绝对 URL）；
 * - 不含反斜杠与控制字符（部分浏览器把 `/\evil.com` 归一化成 `//evil.com`，
 *   换行/制表符也能让 `startsWith('/')` 的检查与真实解析产生分叉）。
 */
const UNSAFE_CHARS = /[\\\u0000-\u001F\u007F]/;

export function safeDeeplink(raw: string | null | undefined): string | null {
  if (!raw) {
    return null;
  }
  const value = raw.trim();
  if (!value.startsWith('/') || value.startsWith('//')) {
    return null;
  }
  if (value.includes('://') || UNSAFE_CHARS.test(value)) {
    return null;
  }
  return value;
}
