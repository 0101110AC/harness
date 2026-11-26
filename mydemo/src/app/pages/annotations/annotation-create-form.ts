import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  afterNextRender,
  input,
  OnInit,
  output,
  signal,
  viewChild,
} from '@angular/core';
import {
  DISCIPLINE_OPTIONS,
  SEVERITY_META,
  SEVERITY_ORDER,
  SHAPE_TYPE_LABEL,
} from '../../core/dictionaries';
import { ShapeType } from '../../core/models/annotation.model';
import { ProjectMemberOut } from '../../core/models/project.model';
import { Discipline, Severity } from '../../core/models/review.model';
import { IconComponent } from '../../shared/icon/icon';

/** 表单提交值：位置由绘制结果带入，不在表单里改（prd-03 §4.3）。 */
export interface AnnotationFormValue {
  title: string;
  content: string | null;
  severity: Severity;
  discipline: Discipline;
  assignee_id: string | null;
  due_date: string | null;
}

const TITLE_MIN = 2;
const TITLE_MAX = 60;
const CONTENT_MAX = 2000;

/**
 * 新建批注表单（prd-03 §4.1 第 2 条：拉框完成后**在面板内联展开**，不做模态弹窗，
 * 因为模态会挡住刚画好的框）。
 *
 * 组件不持开关状态：父级用 `@if (draft())` 控制存在性，关闭即销毁，
 * 下次拉框天然是干净表单（D2 的教训）。
 */
@Component({
  selector: 'app-annotation-create-form',
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <form class="animate-fade-in border-b border-slate-100 bg-primary-soft/40 px-3 py-3" (submit)="onSubmit($event)">
      <div class="mb-2 flex items-center gap-2">
        <app-icon name="plus" [size]="13" class="text-primary" />
        <p class="text-xs font-semibold text-slate-900">新建批注</p>
        <span class="rounded bg-white px-1.5 py-0.5 text-[10px] text-slate-500">
          {{ shapeLabel() }} · {{ locationText() }}
        </span>
        <button
          type="button"
          class="ml-auto text-[11px] text-slate-400 transition-colors hover:text-slate-600"
          (click)="cancelled.emit()"
        >
          取消
        </button>
      </div>

      <label class="mb-1 block text-[11px] font-medium text-slate-600" for="annotation-title">
        标题 <span class="text-danger">*</span>
        <span class="font-normal text-slate-400">（2~60 字）</span>
      </label>
      <input
        #titleInput
        id="annotation-title"
        type="text"
        class="field-input !py-2 text-xs"
        [class.field-input-invalid]="titleInvalid()"
        [attr.maxlength]="titleMax"
        placeholder="例如：3 层疏散宽度不足"
        [value]="title()"
        (input)="onTitle($event)"
        (blur)="titleTouched.set(true)"
      />
      @if (titleInvalid()) {
        <p class="field-error">请输入批注标题（2~60 字）</p>
      }

      <label class="mb-1 mt-2 block text-[11px] font-medium text-slate-600" for="annotation-content">
        描述
      </label>
      <textarea
        id="annotation-content"
        rows="2"
        class="field-input resize-none !py-2 text-xs"
        [attr.maxlength]="contentMax"
        placeholder="补充说明、依据条款…（可选）"
        [value]="content()"
        (input)="onContent($event)"
      ></textarea>

      <p class="mb-1 mt-2 block text-[11px] font-medium text-slate-600">严重度</p>
      <div class="flex flex-wrap gap-1">
        @for (option of severityOptions; track option.value) {
          <button
            type="button"
            class="rounded-md border px-2 py-1 text-[11px] font-medium transition-colors"
            [class]="severityButtonClass(option.value)"
            (click)="severity.set(option.value)"
          >
            {{ option.label }}
          </button>
        }
      </div>

      <div class="mt-2 flex gap-2">
        <div class="min-w-0 flex-1">
          <label class="mb-1 block text-[11px] font-medium text-slate-600" for="annotation-discipline">
            专业 <span class="text-danger">*</span>
          </label>
          <select
            id="annotation-discipline"
            class="field-input !py-1.5 text-xs"
            [value]="discipline()"
            (change)="onDiscipline($event)"
          >
            @for (option of disciplineOptions; track option.value) {
              <option [value]="option.value" [selected]="option.value === discipline()">{{ option.label }}</option>
            }
          </select>
        </div>
        <div class="min-w-0 flex-1">
          <label class="mb-1 block text-[11px] font-medium text-slate-600" for="annotation-assignee">
            指派给 <span class="text-danger">*</span>
          </label>
          <select
            id="annotation-assignee"
            class="field-input !py-1.5 text-xs"
            [class.field-input-invalid]="assigneeInvalid()"
            [value]="assigneeId()"
            (change)="onAssignee($event)"
          >
            <option value="" [selected]="!assigneeId()">请选择责任人</option>
            @for (member of members(); track member.user.id) {
              <option [value]="member.user.id" [selected]="member.user.id === assigneeId()">{{ member.user.name }}</option>
            }
          </select>
        </div>
      </div>
      @if (assigneeInvalid()) {
        <p class="field-error">请选择责任人</p>
      }

      <label class="mb-1 mt-2 block text-[11px] font-medium text-slate-600" for="annotation-due">
        截止日期
      </label>
      <input
        id="annotation-due"
        type="date"
        class="field-input !py-1.5 text-xs"
        [attr.min]="today"
        [value]="dueDate()"
        (change)="onDueDate($event)"
      />

      @if (submitError()) {
        <p class="mt-2 rounded-lg bg-danger-soft px-2 py-1.5 text-[11px] text-danger">{{ submitError() }}</p>
      }

      <div class="mt-3 flex items-center gap-2">
        <button type="submit" class="btn-primary !px-3 !py-1.5 text-xs" [disabled]="busy()">
          {{ busy() ? '提交中…' : '创建批注' }}
        </button>
        <span class="text-[10px] text-slate-400">Esc 取消 · 创建后仍保持工具选中</span>
      </div>
    </form>
  `,
})
export class AnnotationCreateFormComponent implements OnInit {
  readonly shapeType = input.required<ShapeType>();
  /** 文字工具就地输入的内容：直接带进标题，避免用户重复录入 */
  readonly initialTitle = input('');
  readonly locationText = input.required<string>();
  readonly members = input<readonly ProjectMemberOut[]>([]);
  readonly defaultAssigneeId = input<string | null>(null);
  readonly defaultDiscipline = input<Discipline>('GENERAL');
  readonly busy = input(false);
  readonly submitError = input<string | null>(null);

  readonly submitted = output<AnnotationFormValue>();
  readonly cancelled = output<void>();

  protected readonly titleMax = TITLE_MAX;
  protected readonly contentMax = CONTENT_MAX;
  protected readonly today = new Date().toISOString().slice(0, 10);
  protected readonly severityOptions = SEVERITY_ORDER.map((value) => ({
    value,
    label: SEVERITY_META[value].label,
  }));
  protected readonly disciplineOptions = DISCIPLINE_OPTIONS;

  protected readonly title = signal('');
  protected readonly content = signal('');
  protected readonly severity = signal<Severity>('MINOR');
  protected readonly discipline = signal<Discipline>('GENERAL');
  protected readonly assigneeId = signal<string | null>(null);
  protected readonly dueDate = signal('');
  protected readonly titleTouched = signal(false);
  protected readonly assigneeTouched = signal(false);

  private readonly titleInput = viewChild<ElementRef<HTMLInputElement>>('titleInput');

  constructor() {
    // PRD §4.3 要求标题 AutoFocus；用 afterNextRender 而不是 setTimeout(0)
    afterNextRender(() => this.titleInput()?.nativeElement.focus());
  }

  ngOnInit(): void {
    // input() 在 constructor 阶段还没写入，默认值必须放到 ngOnInit 才能安全读取
    this.discipline.set(this.defaultDiscipline());
    this.assigneeId.set(this.defaultAssigneeId());
    this.title.set(this.initialTitle());
  }

  protected shapeLabel(): string {
    return SHAPE_TYPE_LABEL[this.shapeType()];
  }

  protected titleInvalid(): boolean {
    const length = this.title().trim().length;
    return this.titleTouched() && (length < TITLE_MIN || length > TITLE_MAX);
  }

  protected assigneeInvalid(): boolean {
    return this.assigneeTouched() && !this.assigneeId();
  }

  protected severityButtonClass(value: Severity): string {
    const active = this.severity() === value;
    if (!active) {
      return 'border-slate-200 bg-white text-slate-600 hover:border-slate-300';
    }
    return SEVERITY_META[value].text + ' border-current bg-white';
  }

  protected onTitle(event: Event): void {
    this.title.set((event.target as HTMLInputElement).value);
  }

  protected onContent(event: Event): void {
    this.content.set((event.target as HTMLTextAreaElement).value);
  }

  protected onDiscipline(event: Event): void {
    this.discipline.set((event.target as HTMLSelectElement).value as Discipline);
  }

  protected onAssignee(event: Event): void {
    this.assigneeId.set((event.target as HTMLSelectElement).value || null);
  }

  protected onDueDate(event: Event): void {
    this.dueDate.set((event.target as HTMLInputElement).value);
  }

  protected onSubmit(event: Event): void {
    event.preventDefault();
    this.titleTouched.set(true);
    this.assigneeTouched.set(true);
    if (this.busy() || this.titleInvalid() || this.assigneeInvalid()) {
      return;
    }
    this.submitted.emit({
      title: this.title().trim(),
      content: this.content().trim() || null,
      severity: this.severity(),
      discipline: this.discipline(),
      assignee_id: this.assigneeId(),
      due_date: this.dueDate() || null,
    });
  }
}
