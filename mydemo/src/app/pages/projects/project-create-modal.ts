import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  OnDestroy,
  OnInit,
  afterNextRender,
  inject,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { finalize } from 'rxjs';
import { readErrorMessage } from '../../core/api-error';
import { BUILDING_TYPES, DESIGN_STAGES } from '../../core/dictionaries';
import { DesignStage, ProjectCreateRequest, ProjectOut } from '../../core/models/project.model';
import { ProjectService } from '../../core/services/project.service';
import { IconComponent } from '../../shared/icon/icon';

/**
 * 新建项目弹窗。组件本身不持开关状态：父级用 `@if` 创建/销毁，
 * 表单实例随组件一起销毁，下次打开天然是干净状态。
 */
@Component({
  selector: 'app-project-create-modal',
  imports: [ReactiveFormsModule, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:keydown.escape)': 'closed.emit()',
  },
  template: `
    <div
      class="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-950/40 px-4 py-10 backdrop-blur-sm sm:items-center sm:py-6"
      (click)="closed.emit()"
    >
      <div
        class="w-full max-w-md animate-fade-in rounded-card bg-white shadow-float"
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-project-title"
        (click)="$event.stopPropagation()"
      >
        <header class="flex items-start justify-between gap-4 border-b border-slate-100 px-6 py-5">
          <div>
            <h2 id="create-project-title" class="text-base font-semibold text-slate-900">
              新建项目
            </h2>
            <p class="mt-1 text-xs text-slate-400">项目是图纸、批注与预审任务的容器</p>
          </div>
          <button
            type="button"
            class="icon-btn -mr-2 -mt-1"
            aria-label="关闭"
            (click)="closed.emit()"
          >
            <app-icon name="close" [size]="16" />
          </button>
        </header>

        <form [formGroup]="form" (ngSubmit)="submit()" novalidate class="px-6 py-5">
          <div>
            <label for="project-name" class="field-label">项目名称</label>
            <input
              #nameInput
              id="project-name"
              type="text"
              formControlName="name"
              maxlength="128"
              autocomplete="off"
              placeholder="如：滨江办公楼"
              class="field-input"
              [class.field-input-invalid]="nameError() !== null"
            />
            @if (nameError(); as error) {
              <p class="field-error">{{ error }}</p>
            }
          </div>

          <div class="mt-4 grid gap-4 sm:grid-cols-2">
            <div>
              <label for="building-type" class="field-label">业态类型</label>
              <div class="relative">
                <select
                  id="building-type"
                  formControlName="buildingType"
                  class="field-input appearance-none pr-9"
                >
                  <option value="">未指定</option>
                  @for (type of buildingTypes; track type) {
                    <option [value]="type">{{ type }}</option>
                  }
                </select>
                <app-icon
                  name="chevronDown"
                  [size]="14"
                  class="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-slate-400"
                />
              </div>
            </div>

            <div>
              <label for="design-stage" class="field-label">设计阶段</label>
              <div class="relative">
                <select
                  id="design-stage"
                  formControlName="designStage"
                  class="field-input appearance-none pr-9"
                >
                  <option value="">未指定</option>
                  @for (stage of designStages; track stage.value) {
                    <option [value]="stage.value">{{ stage.label }}</option>
                  }
                </select>
                <app-icon
                  name="chevronDown"
                  [size]="14"
                  class="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-slate-400"
                />
              </div>
            </div>
          </div>

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
                创建中
              } @else {
                创建项目
              }
            </button>
          </footer>
        </form>
      </div>
    </div>
  `,
})
export class ProjectCreateModalComponent implements OnInit, OnDestroy {
  private readonly fb = inject(NonNullableFormBuilder);
  private readonly projectService = inject(ProjectService);

  readonly closed = output<void>();
  readonly created = output<ProjectOut>();

  protected readonly buildingTypes = BUILDING_TYPES;
  protected readonly designStages = DESIGN_STAGES;
  protected readonly submitting = signal(false);
  protected readonly errorMessage = signal<string | null>(null);

  protected readonly form = this.fb.group({
    name: this.fb.control('', {
      validators: [Validators.required, Validators.minLength(2), Validators.maxLength(128)],
    }),
    buildingType: this.fb.control(''),
    designStage: this.fb.control<DesignStage | ''>(''),
  });

  private readonly nameInput = viewChild<ElementRef<HTMLInputElement>>('nameInput');

  constructor() {
    afterNextRender(() => this.nameInput()?.nativeElement.focus());
  }

  ngOnInit(): void {
    // 弹窗打开时锁滚动，关闭（组件销毁）时恢复，避免遮罩下页面还能滚
    document.body.style.overflow = 'hidden';
  }

  ngOnDestroy(): void {
    document.body.style.overflow = '';
  }

  protected nameError(): string | null {
    const control = this.form.controls.name;
    if (!control.invalid || !(control.touched || control.dirty)) {
      return null;
    }
    return control.hasError('required') ? '请输入项目名称' : '项目名称需 2~128 个字符';
  }

  protected submit(): void {
    if (this.submitting()) {
      return;
    }
    this.errorMessage.set(null);

    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }

    const { name, buildingType, designStage } = this.form.getRawValue();
    const payload: ProjectCreateRequest = {
      name: name.trim(),
      building_type: buildingType || null,
      design_stage: designStage || null,
    };

    this.submitting.set(true);
    this.projectService
      .create(payload)
      .pipe(finalize(() => this.submitting.set(false)))
      .subscribe({
        next: (project) => this.created.emit(project),
        error: (error: unknown) =>
          this.errorMessage.set(readErrorMessage(error, '创建项目失败，请稍后重试')),
      });
  }
}
