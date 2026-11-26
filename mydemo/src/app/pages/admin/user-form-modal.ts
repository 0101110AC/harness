import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  OnDestroy,
  OnInit,
  afterNextRender,
  computed,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { finalize, map, Observable } from 'rxjs';
import { readApiError, readErrorMessage } from '../../core/api-error';
import {
  AdminUserOut,
  GlobalRole,
  RoleOut,
  UserCreateRequest,
  UserUpdateRequest,
} from '../../core/models/admin.model';
import { AdminService } from '../../core/services/admin.service';
import { IconComponent } from '../../shared/icon/icon';

export interface UserSavedEvent {
  readonly user: AdminUserOut;
  /** 新建用户时带回初始密码，交给父级弹"一次性密码"；编辑时为 null */
  readonly password: string | null;
}

/**
 * 新建 / 编辑用户弹窗（prd-05 §5.2~§5.3）。
 *
 * 角色勾选不走 FormControl：多选框在 reactive form 里要么用 FormArray（下标易错），
 * 要么用 bool 的对象（与后端 `roles: string[]` 多一层转换）。这里用一个
 * `selectedRoles` signal 当唯一状态，提交时读值——代价是"至少选一个"要手动校验。
 */
@Component({
  selector: 'app-user-form-modal',
  imports: [ReactiveFormsModule, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:keydown.escape)': 'closed.emit()',
  },
  template: `
    <div
      class="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto bg-slate-950/40 px-4 py-10 backdrop-blur-sm sm:items-center sm:py-6"
      (click)="closed.emit()"
    >
      <div
        class="w-full max-w-lg animate-fade-in rounded-card bg-white shadow-float"
        role="dialog"
        aria-modal="true"
        aria-labelledby="user-form-title"
        (click)="$event.stopPropagation()"
      >
        <header class="flex items-start justify-between gap-4 border-b border-slate-100 px-6 py-5">
          <div>
            <h2 id="user-form-title" class="text-base font-semibold text-slate-900">
              {{ isCreate() ? '新建用户' : '编辑用户' }}
            </h2>
            <p class="mt-1 text-xs text-slate-400">
              {{ isCreate() ? '创建后即可用该账号登录，初始密码只展示一次' : '可改资料、角色与状态' }}
            </p>
          </div>
          <button type="button" class="icon-btn -mr-2 -mt-1" aria-label="关闭" (click)="closed.emit()">
            <app-icon name="close" [size]="16" />
          </button>
        </header>

        <form [formGroup]="form" (ngSubmit)="submit()" novalidate class="px-6 py-5">
          <div class="grid gap-4 sm:grid-cols-2">
            <div>
              <label for="user-name" class="field-label">用户名</label>
              <input
                #nameInput
                id="user-name"
                type="text"
                formControlName="name"
                maxlength="32"
                autocomplete="off"
                placeholder="如：李工"
                class="field-input"
                [class.field-input-invalid]="nameError() !== null"
              />
              @if (nameError(); as error) {
                <p class="field-error">{{ error }}</p>
              }
            </div>

            <div>
              <label for="user-email" class="field-label">邮箱</label>
              <input
                id="user-email"
                type="email"
                formControlName="email"
                maxlength="128"
                autocomplete="off"
                placeholder="name@company.com"
                class="field-input"
                [class.field-input-invalid]="emailError() !== null"
              />
              @if (emailError(); as error) {
                <p class="field-error">{{ error }}</p>
              }
            </div>
          </div>

          @if (isCreate()) {
            <div class="mt-4">
              <label for="user-password" class="field-label">初始密码</label>
              <div class="flex gap-2">
                <input
                  id="user-password"
                  type="text"
                  formControlName="password"
                  maxlength="64"
                  autocomplete="new-password"
                  placeholder="留空由系统生成"
                  class="field-input font-mono"
                  [class.field-input-invalid]="passwordError() !== null"
                />
                <button
                  type="button"
                  class="btn-secondary shrink-0"
                  title="生成 12 位字母+数字随机密码"
                  (click)="generatePassword()"
                >
                  <app-icon name="refresh" [size]="15" />
                  生成
                </button>
              </div>
              @if (passwordError(); as error) {
                <p class="field-error">{{ error }}</p>
              } @else {
                <p class="mt-1.5 text-[11px] text-slate-400">
                  留空由后端生成强随机密码；手填需 8~64 位且同时包含字母与数字
                </p>
              }
            </div>
          }

          <fieldset class="mt-4">
            <legend class="field-label">全局角色</legend>
            @if (roles().length === 0) {
              <p class="flex items-center gap-1.5 text-xs text-slate-400">
                <app-icon name="loader" [size]="13" class="animate-spin" />
                正在加载角色…
              </p>
            } @else {
              <div class="grid gap-2 sm:grid-cols-2">
                @for (role of roles(); track role.code) {
                  <label
                    class="flex cursor-pointer items-start gap-2 rounded-lg border border-slate-200 px-3 py-2 transition-colors hover:border-slate-300"
                    [class.cursor-not-allowed]="isSelf() && role.code === 'ADMIN'"
                    [class.opacity-60]="isSelf() && role.code === 'ADMIN'"
                    [attr.title]="
                      isSelf() && role.code === 'ADMIN' ? '不能移除自己的管理员角色' : role.description
                    "
                  >
                    <input
                      type="checkbox"
                      class="checkbox mt-0.5"
                      [checked]="selectedRoles().includes(role.code)"
                      [disabled]="isSelf() && role.code === 'ADMIN'"
                      (change)="toggleRole(role.code)"
                    />
                    <span class="min-w-0">
                      <span class="block text-sm text-slate-700">{{ role.name }}</span>
                      <span class="mt-0.5 block truncate text-[11px] text-slate-400">
                        {{ role.description || '—' }}
                      </span>
                    </span>
                  </label>
                }
              </div>
            }
            @if (roleError(); as error) {
              <p class="field-error">{{ error }}</p>
            }
            @if (selectedRoles().includes('ADMIN') && !isSelf()) {
              <p class="mt-2 flex items-start gap-1.5 text-[11px] leading-4 text-warning">
                <app-icon name="alert" [size]="13" class="mt-0.5 shrink-0" />
                管理员可管理所有用户与项目，请谨慎授予
              </p>
            }
          </fieldset>

          <fieldset class="mt-4">
            <legend class="field-label">状态</legend>
            <div class="flex gap-4">
              <label
                class="inline-flex items-center gap-2 text-sm text-slate-600"
                [class.cursor-not-allowed]="isSelf()"
                [attr.title]="isSelf() ? '不能禁用当前登录账号' : null"
              >
                <input
                  type="radio"
                  name="status"
                  class="h-4 w-4 accent-primary"
                  [checked]="status() === 'ACTIVE'"
                  [disabled]="isSelf()"
                  (change)="status.set('ACTIVE')"
                />
                正常
              </label>
              <label
                class="inline-flex items-center gap-2 text-sm text-slate-600"
                [class.cursor-not-allowed]="isSelf()"
                [class.opacity-60]="isSelf()"
                [attr.title]="isSelf() ? '不能禁用当前登录账号' : null"
              >
                <input
                  type="radio"
                  name="status"
                  class="h-4 w-4 accent-primary"
                  [checked]="status() === 'DISABLED'"
                  [disabled]="isSelf()"
                  (change)="status.set('DISABLED')"
                />
                已禁用
              </label>
            </div>
            @if (isSelf()) {
              <p class="mt-1.5 text-[11px] text-slate-400">
                当前登录账号不能禁用自己；管理员角色不可移除
              </p>
            }
          </fieldset>

          @if (errorMessage(); as message) {
            <div
              class="mt-4 flex items-start gap-2 rounded-lg border border-danger/15 bg-danger-soft px-3 py-2.5 text-sm text-danger"
              role="alert"
            >
              <app-icon name="alert" [size]="15" class="mt-0.5" />
              <span class="flex-1 leading-5">{{ message }}</span>
            </div>
          }

          <footer class="mt-6 flex justify-end gap-2">
            <button type="button" class="btn-secondary" (click)="closed.emit()">取消</button>
            <button type="submit" class="btn-primary" [disabled]="submitting()">
              @if (submitting()) {
                <app-icon name="loader" [size]="16" class="animate-spin" />
                {{ isCreate() ? '创建中' : '保存中' }}
              } @else {
                {{ isCreate() ? '创建用户' : '保存修改' }}
              }
            </button>
          </footer>
        </form>
      </div>
    </div>
  `,
})
export class UserFormModalComponent implements OnInit, OnDestroy {
  private readonly fb = inject(NonNullableFormBuilder);
  private readonly admin = inject(AdminService);

  readonly mode = input<'create' | 'edit'>('create');
  readonly user = input<AdminUserOut | null>(null);
  readonly roles = input.required<readonly RoleOut[]>();
  readonly selfId = input<string | null>(null);

  readonly saved = output<UserSavedEvent>();
  readonly closed = output<void>();

  protected readonly submitting = signal(false);
  protected readonly errorMessage = signal<string | null>(null);
  protected readonly roleError = signal<string | null>(null);
  protected readonly selectedRoles = signal<GlobalRole[]>([]);
  protected readonly status = signal<'ACTIVE' | 'DISABLED'>('ACTIVE');

  protected readonly isCreate = computed(() => this.mode() === 'create');
  protected readonly isSelf = computed(() => {
    const current = this.user();
    return current !== null && current.id === this.selfId();
  });

  protected readonly form = this.fb.group({
    name: this.fb.control('', {
      validators: [Validators.required, Validators.minLength(2), Validators.maxLength(32)],
    }),
    email: this.fb.control('', {
      validators: [Validators.required, Validators.email, Validators.maxLength(128)],
    }),
    password: this.fb.control('', {
      // 留空 = 由后端生成强随机密码，所以这里不挂 required
      validators: [Validators.maxLength(64)],
    }),
  });

  private readonly nameInput = viewChild<ElementRef<HTMLInputElement>>('nameInput');

  constructor() {
    afterNextRender(() => this.nameInput()?.nativeElement?.focus());
  }

  ngOnInit(): void {
    document.body.style.overflow = 'hidden';

    const current = this.user();
    if (current) {
      this.form.patchValue({ name: current.name, email: current.email, password: '' });
      this.selectedRoles.set([...current.roles]);
      this.status.set(current.status);
    }
  }

  ngOnDestroy(): void {
    document.body.style.overflow = '';
  }

  protected toggleRole(code: GlobalRole): void {
    if (this.isSelf() && code === 'ADMIN') {
      // 后端硬拦"移除自己的 ADMIN"，这里直接把勾选框锁住，别让用户白点
      return;
    }
    this.selectedRoles.update((list) =>
      list.includes(code) ? list.filter((item) => item !== code) : [...list, code],
    );
    if (this.selectedRoles().length > 0) {
      this.roleError.set(null);
    }
  }

  protected generatePassword(): void {
    const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
    const pick = (): string => alphabet[Math.floor(Math.random() * alphabet.length)];
    // 至少保证 1 个字母 + 1 个数字，避免生成后还被后端规则打回
    const chars = [pick(), '7', ...Array.from({ length: 10 }, pick)];
    for (let i = chars.length - 1; i > 0; i -= 1) {
      const j = Math.floor(Math.random() * (i + 1));
      [chars[i], chars[j]] = [chars[j], chars[i]];
    }
    this.form.controls.password.setValue(chars.join(''));
    this.form.controls.password.markAsDirty();
  }

  protected nameError(): string | null {
    const control = this.form.controls.name;
    if (!control.invalid || !(control.touched || control.dirty)) {
      return null;
    }
    return control.hasError('required') ? '请输入用户名' : '用户名需 2~32 个字符';
  }

  protected emailError(): string | null {
    const control = this.form.controls.email;
    if (!control.invalid || !(control.touched || control.dirty)) {
      return null;
    }
    return control.hasError('required') ? '请输入邮箱' : '邮箱格式不正确';
  }

  protected passwordError(): string | null {
    if (!this.isCreate()) {
      return null;
    }
    const control = this.form.controls.password;
    const value = control.value.trim();
    if (!value) {
      return null; // 留空 = 后端生成
    }
    if (value.length < 8) {
      return '密码至少 8 位，或留空由系统生成';
    }
    if (!this.hasLetter(value) || !this.hasDigit(value)) {
      return '密码需同时包含字母与数字';
    }
    return null;
  }

  protected submit(): void {
    if (this.submitting()) {
      return;
    }
    this.errorMessage.set(null);
    this.roleError.set(null);

    this.form.markAllAsTouched();
    if (this.form.invalid || this.passwordError() !== null) {
      return;
    }
    if (this.selectedRoles().length === 0) {
      this.roleError.set('至少选择一个角色');
      return;
    }

    const { name, email, password } = this.form.getRawValue();
    const roles = this.selectedRoles();
    this.submitting.set(true);

    const request$: Observable<UserSavedEvent> = this.isCreate()
      ? this.admin
          .createUser({
            name: name.trim(),
            email: email.trim().toLowerCase(),
            // 留空时不传字段：后端用 secrets 生成强随机密码并回传 initial_password
            ...(password.trim() ? { password: password.trim() } : {}),
            role_codes: roles,
            status: this.status(),
          } satisfies UserCreateRequest)
          .pipe(map((user) => ({ user: user as AdminUserOut, password: user.initial_password })))
      : this.admin
          .updateUser(this.user()!.id, this.buildUpdate(name, email, roles))
          .pipe(map((user) => ({ user, password: null })));

    request$.pipe(finalize(() => this.submitting.set(false))).subscribe({
      next: (event) => this.saved.emit({ user: event.user, password: event.password }),
      error: (error: unknown) => this.errorMessage.set(this.describeError(error)),
    });
  }

  private buildUpdate(name: string, email: string, roles: GlobalRole[]): UserUpdateRequest {
    const current = this.user();
    const payload: UserUpdateRequest = { name: name.trim(), email: email.trim().toLowerCase() };
    // 自己：ADMIN 必须保留（后端会 422），状态只可能是 ACTIVE，两者都不发
    if (!this.isSelf()) {
      payload.role_codes = roles;
      payload.status = this.status();
    } else {
      payload.role_codes = roles.includes('ADMIN') ? roles : [...roles, 'ADMIN'];
    }
    if (current && current.status === this.status()) {
      delete payload.status;
    }
    return payload;
  }

  private describeError(error: unknown): string {
    const api = readApiError(error);
    const field = api?.detail?.['field'];
    if (field === 'email') {
      return '该邮箱已被注册';
    }
    if (field === 'name') {
      return '该用户名已被使用';
    }
    return readErrorMessage(error, this.isCreate() ? '创建用户失败' : '保存失败');
  }

  private hasLetter(value: string): boolean {
    return /[a-zA-Z]/.test(value);
  }

  private hasDigit(value: string): boolean {
    return /\d/.test(value);
  }
}
