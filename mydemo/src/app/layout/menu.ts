import { MenuItem } from '../core/models/api.model';

/**
 * 已落地的模块路径。菜单由后端下发，但"能不能点进去"是前端能力问题：
 * 未实现的模块统一置灰（纯展示，不弹 Toast），避免用户点进 404 或空白页。
 * D7 起后端下发的五项一级菜单全部可达，这里保留能力位以防后端先上线新菜单。
 */
const IMPLEMENTED_PATHS: ReadonlySet<string> = new Set([
  '/dashboard',
  '/projects',
  '/review',
  '/annotations',
  '/notifications',
  '/admin/users',
  '/admin/roles',
  '/admin/audit',
]);

export function isMenuImplemented(menu: MenuItem): boolean {
  if (menu.path && IMPLEMENTED_PATHS.has(menu.path)) {
    return true;
  }
  return menu.children.some((child) => isMenuImplemented(child));
}

/** 面包屑：按最长前缀匹配当前 URL 对应的菜单标题。 */
export function findMenuTitle(menus: readonly MenuItem[], url: string): string | null {
  const path = url.split('?')[0];
  const matches: { title: string; length: number }[] = [];

  const collect = (items: readonly MenuItem[]): void => {
    for (const item of items) {
      if (item.path && (path === item.path || path.startsWith(`${item.path}/`))) {
        matches.push({ title: item.title, length: item.path.length });
      }
      collect(item.children);
    }
  };

  collect(menus);
  matches.sort((a, b) => b.length - a.length);
  return matches[0]?.title ?? null;
}
