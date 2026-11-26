import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, Router } from '@angular/router';
import { finalize } from 'rxjs';
import { ApiErrorBody } from '../../core/models/api.model';
import { AuthService } from '../../core/services/auth.service';
import { IconComponent, IconName } from '../../shared/icon/icon';

/** 简化版 RFC5322：够用即可，真实格式由后端 EmailStr 兜底。 */
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

interface Feature {
  readonly title: string;
  readonly detail: string;
  readonly icon: IconName;
}

@Component({
  selector: 'app-login',
  imports: [ReactiveFormsModule, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './login.html',
})
export class LoginComponent {
  private readonly fb = inject(NonNullableFormBuilder);
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  protected readonly form = this.fb.group({
    email: this.fb.control('', {
      validators: [Validators.required, Validators.pattern(EMAIL_PATTERN)],
    }),
    password: this.fb.control('', {
      validators: [Validators.required, Validators.minLength(8), Validators.maxLength(64)],
    }),
    rememberMe: this.fb.control(true),
  });

  protected readonly submitting = signal(false);
  protected readonly errorMessage = signal<string | null>(null);

  protected readonly features: readonly Feature[] = [
    {
      title: '规范驱动的预审',
      detail: '内置强条与地方标准，逐条比对，输出结构化问题清单。',
      icon: 'check',
    },
    {
      title: '问题可定位',
      detail: '批注锚定图面坐标，点开即跳到真正出问题的位置。',
      icon: 'review',
    },
    {
      title: '协作闭环',
      detail: '下发、修改、复核全流程留痕，谁在何时关闭都有据可查。',
      icon: 'annotation',
    },
  ];

  protected submit(): void {
    if (this.submitting()) {
      return;
    }

    this.errorMessage.set(null);

    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }

    const { email, password, rememberMe } = this.form.getRawValue();
    this.submitting.set(true);

    this.auth
      .login(email, password, rememberMe)
      .pipe(finalize(() => this.submitting.set(false)))
      .subscribe({
        next: () => {
          void this.router.navigateByUrl(this.redirectTarget(), { replaceUrl: true });
        },
        error: (error: unknown) => {
          this.errorMessage.set(describeLoginError(error));
        },
      });
  }

  protected showError(field: 'email' | 'password'): boolean {
    const control = this.form.controls[field];
    return control.invalid && (control.touched || control.dirty);
  }

  protected emailError(): string | null {
    if (!this.showError('email')) {
      return null;
    }
    return this.form.controls.email.hasError('required')
      ? '请输入邮箱'
      : '请输入有效的邮箱地址';
  }

  protected passwordError(): string | null {
    if (!this.showError('password')) {
      return null;
    }
    const control = this.form.controls.password;
    if (control.hasError('required')) {
      return '请输入密码';
    }
    if (control.hasError('minlength') || control.hasError('maxlength')) {
      return '密码需 8~64 位';
    }
    return null;
  }

  /** 登录后回到被守卫拦下的原地址；只接受站内绝对路径，防开放重定向。 */
  private redirectTarget(): string {
    const redirect = this.route.snapshot.queryParamMap.get('redirect');
    if (
      !redirect ||
      !redirect.startsWith('/') ||
      redirect.startsWith('//') ||
      redirect.startsWith('/login')
    ) {
      return '/dashboard';
    }
    return redirect;
  }
}

function describeLoginError(error: unknown): string {
  if (!(error instanceof HttpErrorResponse)) {
    return '登录失败，请稍后重试';
  }

  if (error.status === 0) {
    return '网络异常，请检查网络连接后重试';
  }

  const body = readErrorBody(error);

  if (body?.code === 'INVALID_CREDENTIALS') {
    // 不区分邮箱/密码错误，防账号枚举（prd-01 §4.1）
    return '邮箱或密码错误';
  }
  if (body?.code === 'RATE_LIMITED' || error.status === 429) {
    return '尝试过于频繁，请 5 分钟后再试';
  }
  if (body?.code === 'ACCOUNT_DISABLED') {
    return body.message || '账号已被禁用，请联系管理员';
  }

  return body?.message || '登录失败，请稍后重试';
}

function readErrorBody(error: HttpErrorResponse): ApiErrorBody | null {
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
