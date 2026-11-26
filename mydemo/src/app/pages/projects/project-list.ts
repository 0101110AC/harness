import { ChangeDetectionStrategy, Component, computed, effect, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router } from '@angular/router';
import { finalize } from 'rxjs';
import { readErrorMessage } from '../../core/api-error';
import { designStageLabel } from '../../core/dictionaries';
import { ProjectOut } from '../../core/models/project.model';
import { ProjectQuery, ProjectService } from '../../core/services/project.service';
import { AuthService } from '../../core/services/auth.service';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { IconComponent } from '../../shared/icon/icon';
import { PromptDialogComponent } from '../../shared/prompt-dialog';
import { formatAbsoluteTime, formatRelativeTime } from '../../shared/time';
import { ProjectCreateModalComponent } from './project-create-modal';

type ProjectAction = '重命名' | '归档' | '删除';

interface ListFilter {
  readonly key: 'pending-review' | 'open' | 'closed-this-week';
  readonly label: string;
}

@Component({
  selector: 'app-project-list',
  imports: [
    IconComponent,
    ProjectCreateModalComponent,
    PromptDialogComponent,
    ConfirmDialogComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './project-list.html',
})
export class ProjectListComponent {
  private readonly projectService = inject(ProjectService);
  private readonly auth = inject(AuthService);
  private readonly toast = inject(ToastService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  /** 按钮级权限（prd-05 §4.4）：没有 project:write 就不渲染新建与行操作，别等 403 */
  protected readonly canWrite = computed(() => this.auth.hasPermission('project:write'));

  protected readonly projects = signal<readonly ProjectOut[]>([]);
  protected readonly total = signal(0);
  protected readonly loading = signal(true);
  protected readonly error = signal(false);
  protected readonly createOpen = signal(false);
  protected readonly actionMenuId = signal<string | null>(null);
  protected readonly renameTarget = signal<ProjectOut | null>(null);
  protected readonly archiveTarget = signal<ProjectOut | null>(null);
  protected readonly deleteTarget = signal<ProjectOut | null>(null);
  protected readonly mutating = signal(false);

  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });

  /** 统计卡下钻参数 → 可读筛选项（prd-01 §4.3 的 deeplink 口径）。 */
  protected readonly activeFilter = computed<ListFilter | null>(() => {
    const params = this.queryParams();
    if (params.get('filter') === 'pending-review') {
      return { key: 'pending-review', label: '待审图纸' };
    }
    if (params.get('status') === 'open') {
      return { key: 'open', label: '未关闭批注' };
    }
    if (params.get('closed_this_week') === '1') {
      return { key: 'closed-this-week', label: '本周确认问题' };
    }
    return null;
  });

  constructor() {
    // 只依赖 activeFilter：地址变化 → 重新拉列表；避免在 effect 里读自己写的 loading/total
    effect(() => {
      const filter = this.activeFilter();
      this.load(filter);
    });
  }

  protected load(filter: ListFilter | null = this.activeFilter()): void {
    const query: ProjectQuery = { sort: 'recent', pageSize: 24 };
    if (filter?.key === 'pending-review') {
      query.filter = 'pending-review';
    }
    if (filter?.key === 'open') {
      query.status = 'open';
    }
    if (filter?.key === 'closed-this-week') {
      query.closedThisWeek = true;
    }

    this.loading.set(true);
    this.error.set(false);
    this.projectService
      .getAll(query)
      .pipe(finalize(() => this.loading.set(false)))
      .subscribe({
        next: (page) => {
          this.projects.set(page.items);
          this.total.set(page.total);
        },
        error: () => this.error.set(true),
      });
  }

  protected clearFilter(): void {
    void this.router.navigate(['/projects']);
  }

  protected openCreate(): void {
    this.actionMenuId.set(null);
    this.createOpen.set(true);
  }

  protected closeCreate(): void {
    this.createOpen.set(false);
  }

  protected onCreated(project: ProjectOut): void {
    this.createOpen.set(false);
    this.toast.success(`项目「${project.name}」已创建`);
    // 新建项目按 updated_at 排在最前，重新拉列表保证与后端口径一致
    this.load();
  }

  protected toggleMenu(id: string, event: Event): void {
    event.stopPropagation();
    this.actionMenuId.update((current) => (current === id ? null : id));
  }

  protected closeMenu(): void {
    this.actionMenuId.set(null);
  }

  protected onAction(project: ProjectOut, action: ProjectAction): void {
    this.actionMenuId.set(null);
    switch (action) {
      case '重命名':
        this.renameTarget.set(project);
        return;
      case '归档':
        this.archiveTarget.set(project);
        return;
      case '删除':
        this.deleteTarget.set(project);
        return;
    }
  }

  /* ---------- 重命名 / 归档 / 删除：真实接口，不再是占位 Toast ---------- */

  protected closeDialogs(): void {
    this.renameTarget.set(null);
    this.archiveTarget.set(null);
    this.deleteTarget.set(null);
  }

  protected submitRename(name: string): void {
    const target = this.renameTarget();
    if (!target || this.mutating()) {
      return;
    }
    this.mutating.set(true);
    this.projectService
      .update(target.id, { name })
      .pipe(finalize(() => this.mutating.set(false)))
      .subscribe({
        next: (project) => {
          this.renameTarget.set(null);
          this.toast.success(`已重命名为「${project.name}」`);
          this.load();
        },
        error: (error: unknown) => {
          this.renameTarget.set(null);
          this.toast.error(readErrorMessage(error, '重命名失败，请重试'));
        },
      });
  }

  protected confirmArchive(): void {
    const target = this.archiveTarget();
    if (!target || this.mutating()) {
      return;
    }
    this.mutating.set(true);
    this.projectService
      .update(target.id, { status: 'ARCHIVED' })
      .pipe(finalize(() => this.mutating.set(false)))
      .subscribe({
        next: () => {
          this.archiveTarget.set(null);
          this.toast.success(`「${target.name}」已归档`);
          this.load();
        },
        error: (error: unknown) => {
          this.archiveTarget.set(null);
          this.toast.error(readErrorMessage(error, '归档失败，请重试'));
        },
      });
  }

  protected confirmDelete(): void {
    const target = this.deleteTarget();
    if (!target || this.mutating()) {
      return;
    }
    this.mutating.set(true);
    this.projectService
      .remove(target.id)
      .pipe(finalize(() => this.mutating.set(false)))
      .subscribe({
        next: () => {
          this.deleteTarget.set(null);
          this.toast.success(`「${target.name}」已删除`);
          this.load();
        },
        error: (error: unknown) => {
          this.deleteTarget.set(null);
          this.toast.error(readErrorMessage(error, '删除失败，请重试'));
        },
      });
  }

  protected openProject(project: ProjectOut): void {
    void this.router.navigate(['/projects', project.id]);
  }

  protected stageLabel(stage: string | null): string {
    return designStageLabel(stage);
  }

  protected timeAgo(iso: string): string {
    return formatRelativeTime(iso);
  }

  protected absoluteTime(iso: string): string {
    return formatAbsoluteTime(iso);
  }
}
