import { inject } from '@angular/core';
import { Router, Routes } from '@angular/router';
import { authGuard } from './core/guards/auth.guard';
import { permissionGuard } from './core/guards/permission.guard';

export const routes: Routes = [
  {
    path: 'login',
    title: '登录 · 建筑 AI 预审工作台',
    loadComponent: () => import('./pages/login/login').then((m) => m.LoginComponent),
  },
  {
    // 403 必须在守卫之外：否则守卫再次判定无权限会形成 /403 → /403 循环
    path: '403',
    title: '无权限访问 · 建筑 AI 预审工作台',
    loadComponent: () => import('./pages/forbidden/forbidden').then((m) => m.ForbiddenComponent),
  },
  {
    path: '',
    canActivate: [authGuard],
    loadComponent: () => import('./layout/app-layout').then((m) => m.AppLayoutComponent),
    children: [
      { path: '', pathMatch: 'full', redirectTo: 'dashboard' },
      {
        path: 'dashboard',
        title: '工作台 · 建筑 AI 预审工作台',
        loadComponent: () =>
          import('./pages/dashboard/dashboard').then((m) => m.DashboardComponent),
      },
      {
        path: 'projects',
        title: '项目管理 · 建筑 AI 预审工作台',
        loadComponent: () =>
          import('./pages/projects/project-list').then((m) => m.ProjectListComponent),
      },
      {
        path: 'projects/:pid/drawings/:did/review',
        title: '预审中心 · 建筑 AI 预审工作台',
        loadComponent: () =>
          import('./pages/review/review-center').then((m) => m.ReviewCenterComponent),
      },
      {
        // 卡点二的落地页：整页而不是弹窗，报告要读完才能确认
        path: 'projects/:pid/drawings/:did/review/:tid/report',
        title: '报告初稿 · 建筑 AI 预审工作台',
        loadComponent: () =>
          import('./pages/review/report-confirm').then((m) => m.ReportConfirmComponent),
      },
      {
        // 图纸批注主页：48px 工具条 + 画布 + 380px 批注面板（prd-03 §3.1）
        path: 'projects/:pid/drawings/:did',
        title: '图纸批注 · 建筑 AI 预审工作台',
        loadComponent: () =>
          import('./pages/annotations/annotation-canvas').then((m) => m.AnnotationCanvasComponent),
      },
      {
        // 预审中心没有独立列表页：菜单点进来落到"待审图纸"筛选，避免置灰挡路
        path: 'review',
        pathMatch: 'full',
        redirectTo: () =>
          inject(Router).createUrlTree(['/projects'], { queryParams: { filter: 'pending-review' } }),
      },
      {
        path: 'annotations',
        title: '批注协同 · 建筑 AI 预审工作台',
        loadComponent: () =>
          import('./pages/annotations/annotation-list').then((m) => m.AnnotationListComponent),
      },
      {
        path: 'admin',
        pathMatch: 'full',
        redirectTo: 'admin/users',
      },
      {
        path: 'admin/users',
        title: '用户管理 · 建筑 AI 预审工作台',
        canActivate: [permissionGuard('system:user:manage')],
        loadComponent: () =>
          import('./pages/admin/user-management').then((m) => m.UserManagementComponent),
      },
      {
        path: 'admin/roles',
        title: '角色权限 · 建筑 AI 预审工作台',
        canActivate: [permissionGuard('system:role:read')],
        loadComponent: () =>
          import('./pages/admin/role-permissions').then((m) => m.RolePermissionsComponent),
      },
      {
        path: 'admin/audit',
        title: '审计日志 · 建筑 AI 预审工作台',
        canActivate: [permissionGuard('system:audit:read')],
        loadComponent: () =>
          import('./pages/admin/audit-log').then((m) => m.AuditLogComponent),
      },
      {
        path: 'notifications',
        title: '消息中心 · 建筑 AI 预审工作台',
        loadComponent: () =>
          import('./pages/notifications/notification-center').then(
            (m) => m.NotificationCenterComponent,
          ),
      },
      {
        path: 'settings/notifications',
        title: '通知偏好 · 建筑 AI 预审工作台',
        loadComponent: () =>
          import('./pages/settings/notification-preferences').then(
            (m) => m.NotificationPreferencesComponent,
          ),
      },
      {
        path: 'projects/:id',
        title: '项目详情 · 建筑 AI 预审工作台',
        loadComponent: () =>
          import('./pages/projects/project-detail').then((m) => m.ProjectDetailComponent),
      },
    ],
  },
  { path: '**', redirectTo: '' },
];
