import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { finalize } from 'rxjs';
import { readErrorMessage } from '../../core/api-error';
import {
  AdminUserOut,
  GlobalRole,
  RoleOut,
  UserStatus,
} from '../../core/models/admin.model';
import { AdminService } from '../../core/services/admin.service';
import { AuthService } from '../../core/services/auth.service';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { IconComponent } from '../../shared/icon/icon';
import { formatAbsoluteTime, formatRelativeTime } from '../../shared/time';
import { PasswordRevealModalComponent } from './password-reveal-modal';
import { UserFormModalComponent } from './user-form-modal';

const PAGE_SIZE = 20;
const KEYWORD_DEBOUNCE_MS = 300;

interface PasswordInfo {
  readonly title: string;
  readonly username: string;
  readonly password: string;
  readonly note: string | null;
}

/** 用户管理（prd-05 §5）。列表 + 新建 + 编辑 + 重置密码 + 禁用/启用。 */
@Component({
  selector: 'app-user-management',
  imports: [
    IconComponent,
    ConfirmDialogComponent,
    UserFormModalComponent,
    PasswordRevealModalComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './user-management.html',
})
export class UserManagementComponent {
  private readonly admin = inject(AdminService);
  private readonly auth = inject(AuthService);
  private readonly toast = inject(ToastService);

  protected readonly users = signal<readonly AdminUserOut[]>([]);
  protected readonly roles = signal<readonly RoleOut[]>([]);
  protected readonly rolesLoading = signal(true);
  protected readonly rolesError = signal(false);
  protected readonly total = signal(0);
  protected readonly page = signal(1);
  protected readonly pages = signal(1);
  protected readonly loading = signal(true);
  protected readonly loadError = signal(false);

  protected readonly keyword = signal('');
  protected readonly roleFilter = signal<GlobalRole | ''>('');
  protected readonly statusFilter = signal<UserStatus | ''>('');

  protected readonly createOpen = signal(false);
  protected readonly editUser = signal<AdminUserOut | null>(null);
  protected readonly resetTarget = signal<AdminUserOut | null>(null);
  protected readonly statusTarget = signal<AdminUserOut | null>(null);
  protected readonly passwordInfo = signal<PasswordInfo | null>(null);
  protected readonly rowBusyId = signal<string | null>(null);

  protected readonly selfId = computed(() => this.auth.currentUser()?.id ?? null);
  protected readonly hasFilter = computed(
    () => this.keyword() !== '' || this.roleFilter() !== '' || this.statusFilter() !== '',
  );
  protected readonly rangeStart = computed(() =>
    this.total() === 0 ? 0 : (this.page() - 1) * PAGE_SIZE + 1,
  );
  protected readonly rangeEnd = computed(() => Math.min(this.page() * PAGE_SIZE, this.total()));

  private keywordTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    this.loadRoles();
    this.load();
  }

  /* ---------- 数据 ---------- */

  private loadRoles(): void {
    this.rolesLoading.set(true);
    this.admin
      .listRoles()
      .pipe(finalize(() => this.rolesLoading.set(false)))
      .subscribe({
        next: (roles) => {
          this.roles.set(roles);
          this.rolesError.set(false);
        },
        // 角色接口失败不阻断列表：筛选下拉留空，行内角色标签仍用后端下发的 role_names
        error: () => {
          this.roles.set([]);
          this.rolesError.set(true);
        },
      });
  }

  protected load(): void {
    this.loading.set(true);
    this.loadError.set(false);
    this.admin
      .listUsers({
        keyword: this.keyword(),
        role: this.roleFilter() || null,
        status: this.statusFilter() || null,
        page: this.page(),
        pageSize: PAGE_SIZE,
      })
      .pipe(finalize(() => this.loading.set(false)))
      .subscribe({
        next: (result) => {
          this.users.set(result.items ?? []);
          this.total.set(result.total ?? 0);
          this.pages.set(Math.max(1, result.pages ?? 1));
        },
        error: () => this.loadError.set(true),
      });
  }

  protected reloadFromFirstPage(): void {
    this.page.set(1);
    this.load();
  }

  protected onKeywordInput(event: Event): void {
    const value = (event.target as HTMLInputElement).value;
    if (this.keywordTimer !== null) {
      clearTimeout(this.keywordTimer);
    }
    this.keywordTimer = setTimeout(() => {
      this.keywordTimer = null;
      this.keyword.set(value);
      this.reloadFromFirstPage();
    }, KEYWORD_DEBOUNCE_MS);
  }

  protected onRoleChange(event: Event): void {
    this.roleFilter.set((event.target as HTMLSelectElement).value as GlobalRole | '');
    this.reloadFromFirstPage();
  }

  protected onStatusChange(event: Event): void {
    this.statusFilter.set((event.target as HTMLSelectElement).value as UserStatus | '');
    this.reloadFromFirstPage();
  }

  protected clearFilters(): void {
    this.keyword.set('');
    this.roleFilter.set('');
    this.statusFilter.set('');
    this.reloadFromFirstPage();
  }

  protected prevPage(): void {
    if (this.page() <= 1) {
      return;
    }
    this.page.update((value) => value - 1);
    this.load();
  }

  protected nextPage(): void {
    if (this.page() >= this.pages()) {
      return;
    }
    this.page.update((value) => value + 1);
    this.load();
  }

  /* ---------- 行操作 ---------- */

  protected openCreate(): void {
    // 角色还在路上时也允许打开：弹窗里的勾选项会随 roles 信号到达自动补上。
    // 只有"确实失败了"才提示，避免刚进页面就点按钮被误报"角色清单加载失败"。
    if (this.rolesError()) {
      this.toast.error('角色清单加载失败，请刷新页面后重试');
      return;
    }
    this.createOpen.set(true);
  }

  protected closeCreate(): void {
    this.createOpen.set(false);
  }

  protected openEdit(user: AdminUserOut): void {
    this.editUser.set(user);
  }

  protected closeEdit(): void {
    this.editUser.set(null);
  }

  protected onSaved(event: { user: AdminUserOut; password: string | null }): void {
    const isCreate = this.createOpen();
    this.createOpen.set(false);
    this.editUser.set(null);

    if (event.password) {
      this.passwordInfo.set({
        title: '用户已创建',
        username: event.user.email,
        password: event.password,
        note: '请把初始密码交给用户，并提醒其登录后自行修改。',
      });
    } else {
      this.toast.success(`已保存「${event.user.name}」的资料`);
    }
    if (!isCreate) {
      this.load();
    } else {
      this.reloadFromFirstPage();
    }
  }

  protected askReset(user: AdminUserOut): void {
    this.resetTarget.set(user);
  }

  protected cancelReset(): void {
    this.resetTarget.set(null);
  }

  protected confirmReset(): void {
    const target = this.resetTarget();
    if (!target || this.rowBusyId()) {
      return;
    }
    this.rowBusyId.set(target.id);
    this.admin
      .resetPassword(target.id)
      .pipe(finalize(() => this.rowBusyId.set(null)))
      .subscribe({
        next: (result) => {
          this.resetTarget.set(null);
          this.passwordInfo.set({
            title: '密码已重置',
            username: target.email,
            password: result.new_password,
            note:
              target.status === 'DISABLED'
                ? '该账号处于禁用状态，重置后仍需启用才能登录；其原有登录态已失效。'
                : '该用户原有登录态已失效，需要用新密码重新登录。',
          });
          this.load();
        },
        error: (error: unknown) =>
          this.toast.error(readErrorMessage(error, '重置密码失败，请重试')),
      });
  }

  protected askToggleStatus(user: AdminUserOut): void {
    this.statusTarget.set(user);
  }

  protected cancelToggleStatus(): void {
    this.statusTarget.set(null);
  }

  protected confirmToggleStatus(): void {
    const target = this.statusTarget();
    if (!target || this.rowBusyId()) {
      return;
    }
    const next: UserStatus = target.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE';
    this.rowBusyId.set(target.id);
    this.admin
      .setStatus(target.id, next)
      .pipe(finalize(() => this.rowBusyId.set(null)))
      .subscribe({
        next: (user) => {
          this.statusTarget.set(null);
          this.users.update((list) => list.map((row) => (row.id === user.id ? user : row)));
          this.toast.success(
            next === 'DISABLED' ? `已禁用「${user.name}」` : `已启用「${user.name}」`,
          );
          this.load();
        },
        error: (error: unknown) => {
          this.statusTarget.set(null);
          this.toast.error(readErrorMessage(error, '操作失败，请重试'));
        },
      });
  }

  /* ---------- 展示 ---------- */

  protected isSelf(user: AdminUserOut): boolean {
    return user.id === this.selfId();
  }

  protected initial(user: AdminUserOut): string {
    return Array.from(user.name.trim())[0]?.toUpperCase() ?? '?';
  }

  protected absolute(iso: string | null): string {
    return iso ? formatAbsoluteTime(iso) : '从未登录';
  }

  protected relative(iso: string | null): string {
    return iso ? formatRelativeTime(iso) : '从未登录';
  }

  protected roleTone(code: GlobalRole): string {
    switch (code) {
      case 'ADMIN':
        return 'bg-primary-soft text-primary';
      case 'REVIEWER':
        return 'bg-success-soft text-success';
      case 'DESIGNER':
        return 'bg-violet-50 text-violet-600';
      default:
        return 'bg-slate-100 text-slate-500';
    }
  }
}
