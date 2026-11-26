import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  signal,
} from '@angular/core';
import { RouterLink, Router } from '@angular/router';
import { finalize } from 'rxjs';
import { readErrorMessage } from '../../core/api-error';
import {
  DRAWING_STATUS_META,
  FILE_TYPE_BADGE,
  FILE_TYPE_LABEL,
  PROJECT_ROLE_LABEL,
  RE_REVIEWABLE_STATUSES,
  SEVERITY_META,
  StatusMeta,
  designStageLabel,
} from '../../core/dictionaries';
import { DrawingOut, FileType, SeverityKey } from '../../core/models/drawing.model';
import { ProjectDetailOut, ProjectRole } from '../../core/models/project.model';
import { DrawingService } from '../../core/services/drawing.service';
import { ProjectService } from '../../core/services/project.service';
import { ToastService } from '../../core/services/toast.service';
import { formatDateTime, formatFileSize } from '../../shared/bytes';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { IconComponent } from '../../shared/icon/icon';
import { UploadModalComponent } from './upload-modal';

interface IssueCell {
  readonly label: string;
  readonly count: number;
  readonly text: string;
  readonly dot: string;
}

const SEVERITY_ORDER: readonly SeverityKey[] = ['BLOCKER', 'MAJOR', 'MINOR', 'INFO'];
const SEVERITY_FALLBACK: StatusMeta = {
  label: '未知状态',
  dot: 'bg-slate-300',
  text: 'text-slate-400',
};

/**
 * 项目详情：图纸列表 + 成员管理。
 *
 * `id` 由 `withComponentInputBinding()` 从路由注入；在 `/projects/a` → `/projects/b`
 * 之间切换时组件被复用，靠 effect 监听 id 变化重新拉数据，不依赖构造函数只跑一次。
 */
@Component({
  selector: 'app-project-detail',
  imports: [RouterLink, IconComponent, UploadModalComponent, ConfirmDialogComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './project-detail.html',
})
export class ProjectDetailComponent {
  private readonly projectService = inject(ProjectService);
  private readonly drawingService = inject(DrawingService);
  private readonly toast = inject(ToastService);
  private readonly router = inject(Router);

  readonly id = input.required<string>();

  protected readonly project = signal<ProjectDetailOut | null>(null);
  protected readonly projectLoading = signal(true);
  protected readonly projectError = signal(false);

  protected readonly drawings = signal<readonly DrawingOut[]>([]);
  protected readonly drawingsLoading = signal(true);
  protected readonly drawingsError = signal(false);
  protected readonly drawingsTotal = signal(0);

  protected readonly activeTab = signal<'drawings' | 'members'>('drawings');
  protected readonly uploadOpen = signal(false);
  protected readonly pendingDelete = signal<DrawingOut | null>(null);
  protected readonly pendingReReview = signal<DrawingOut | null>(null);
  protected readonly actionBusy = signal(false);
  protected readonly downloadingId = signal<string | null>(null);

  protected readonly memberCount = computed(() => this.project()?.members.length ?? 0);

  constructor() {
    effect(() => {
      const projectId = this.id();
      this.loadProject(projectId);
      this.loadDrawings(projectId);
    });
  }

  /* ---------- 数据 ---------- */

  private loadProject(projectId: string): void {
    this.projectLoading.set(true);
    this.projectError.set(false);
    this.projectService
      .getById(projectId)
      .pipe(finalize(() => this.projectLoading.set(false)))
      .subscribe({
        next: (project) => this.project.set(project),
        error: () => this.projectError.set(true),
      });
  }

  protected loadDrawings(projectId = this.id()): void {
    this.drawingsLoading.set(true);
    this.drawingsError.set(false);
    this.drawingService
      .list(projectId, { pageSize: 50 })
      .pipe(finalize(() => this.drawingsLoading.set(false)))
      .subscribe({
        next: (page) => {
          this.drawings.set(page.items);
          this.drawingsTotal.set(page.total);
        },
        error: () => this.drawingsError.set(true),
      });
  }

  protected onUploaded(): void {
    this.loadDrawings();
    this.loadProject(this.id());
  }

  /** 失败重试：项目信息与图纸一起重拉，避免只刷新一半。 */
  protected reload(): void {
    this.loadProject(this.id());
    this.loadDrawings();
  }

  /* ---------- 行操作 ---------- */

  protected viewReview(drawing: DrawingOut): void {
    void this.router.navigate(['/projects', this.id(), 'drawings', drawing.id, 'review']);
  }

  /** 图纸批注主页（D5）：画布 + 批注面板 */
  protected viewAnnotations(drawing: DrawingOut): void {
    void this.router.navigate(['/projects', this.id(), 'drawings', drawing.id]);
  }

  protected download(drawing: DrawingOut): void {
    if (this.downloadingId()) {
      return;
    }
    this.downloadingId.set(drawing.id);
    this.drawingService
      .getViewUrl(drawing.id)
      .pipe(finalize(() => this.downloadingId.set(null)))
      .subscribe({
        next: (view) => {
          // 短时签名 URL：只在本窗口打开，不做拼接与缓存
          window.open(view.url, '_blank', 'noopener');
        },
        error: (error: unknown) => this.toast.error(readErrorMessage(error, '获取下载地址失败')),
      });
  }

  protected canReReview(drawing: DrawingOut): boolean {
    return RE_REVIEWABLE_STATUSES.has(drawing.status);
  }

  protected confirmDelete(): void {
    const drawing = this.pendingDelete();
    if (!drawing) {
      return;
    }
    this.actionBusy.set(true);
    this.drawingService
      .remove(drawing.id)
      .pipe(finalize(() => this.actionBusy.set(false)))
      .subscribe({
        next: () => {
          this.pendingDelete.set(null);
          this.toast.success(`「${drawing.name}」已删除`);
          this.onUploaded();
        },
        error: (error: unknown) => this.toast.error(readErrorMessage(error, '删除失败，请稍后重试')),
      });
  }

  protected confirmReReview(): void {
    const drawing = this.pendingReReview();
    if (!drawing) {
      return;
    }
    this.actionBusy.set(true);
    this.drawingService
      .createReviewTask(drawing.id)
      .pipe(finalize(() => this.actionBusy.set(false)))
      .subscribe({
        next: (task) => {
          this.pendingReReview.set(null);
          this.toast.success(`已发起预审，任务 ${task.id.slice(0, 8)} 已进入队列`);
          this.loadDrawings();
        },
        error: (error: unknown) => this.toast.error(readErrorMessage(error, '发起预审失败')),
      });
  }

  /* ---------- 展示辅助 ---------- */

  protected stageLabel(stage: string | null): string {
    return designStageLabel(stage);
  }

  protected typeLabel(fileType: FileType): string {
    return FILE_TYPE_LABEL[fileType];
  }

  protected typeBadge(fileType: FileType): string {
    return FILE_TYPE_BADGE[fileType];
  }

  protected statusMeta(drawing: DrawingOut): StatusMeta {
    return DRAWING_STATUS_META[drawing.status] ?? SEVERITY_FALLBACK;
  }

  protected issueCells(drawing: DrawingOut): readonly IssueCell[] {
    const stat = drawing.issue_stat;
    if (!stat) {
      return [];
    }
    return SEVERITY_ORDER.flatMap((key) => {
      const count = stat[key] ?? 0;
      if (count <= 0) {
        return [];
      }
      const meta = SEVERITY_META[key];
      return [{ label: meta.label, count, text: meta.text, dot: meta.dot }];
    });
  }

  protected issueTitle(drawing: DrawingOut): string {
    const cells = this.issueCells(drawing);
    return cells.length === 0 ? '未预审' : cells.map((cell) => `${cell.label} ${cell.count}`).join(' · ');
  }

  protected sizeText(bytes: number): string {
    return formatFileSize(bytes);
  }

  protected dateTime(iso: string): string {
    return formatDateTime(iso);
  }

  protected initials(name: string): string {
    return name.trim() ? Array.from(name.trim())[0].toUpperCase() : '?';
  }

  protected roleLabel(role: ProjectRole): string {
    return PROJECT_ROLE_LABEL[role];
  }
}
