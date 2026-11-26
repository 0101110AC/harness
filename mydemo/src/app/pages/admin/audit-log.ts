import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { finalize } from 'rxjs';
import { AuditActionOption, AuditLogOut } from '../../core/models/admin.model';
import { AdminService } from '../../core/services/admin.service';
import { IconComponent } from '../../shared/icon/icon';
import { formatAbsoluteTime } from '../../shared/time';

const PAGE_SIZE = 20;

/** 审计日志（prd-05 §8）：只读、可筛选、不提供任何删除入口。 */
@Component({
  selector: 'app-audit-log',
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './audit-log.html',
})
export class AuditLogComponent {
  private readonly admin = inject(AdminService);

  protected readonly logs = signal<readonly AuditLogOut[]>([]);
  protected readonly actions = signal<readonly AuditActionOption[]>([]);
  protected readonly total = signal(0);
  protected readonly page = signal(1);
  protected readonly pages = signal(1);
  protected readonly loading = signal(true);
  protected readonly loadError = signal(false);

  protected readonly actionFilter = signal('');
  protected readonly startDate = signal('');
  protected readonly endDate = signal('');

  protected readonly hasFilter = computed(
    () => this.actionFilter() !== '' || this.startDate() !== '' || this.endDate() !== '',
  );
  protected readonly rangeStart = computed(() =>
    this.total() === 0 ? 0 : (this.page() - 1) * PAGE_SIZE + 1,
  );
  protected readonly rangeEnd = computed(() => Math.min(this.page() * PAGE_SIZE, this.total()));

  constructor() {
    this.loadActions();
    this.load();
  }

  private loadActions(): void {
    this.admin.listAuditActions().subscribe({
      next: (actions) => this.actions.set(actions),
      // 枚举失败不影响列表本身，筛选下拉留空即可
      error: () => this.actions.set([]),
    });
  }

  protected load(): void {
    this.loading.set(true);
    this.loadError.set(false);
    this.admin
      .listAuditLogs({
        action: this.actionFilter() || null,
        start: this.startDate() ? startOfDay(this.startDate()) : null,
        end: this.endDate() ? endOfDay(this.endDate()) : null,
        page: this.page(),
        pageSize: PAGE_SIZE,
      })
      .pipe(finalize(() => this.loading.set(false)))
      .subscribe({
        next: (result) => {
          this.logs.set(result.items ?? []);
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

  protected onActionChange(event: Event): void {
    this.actionFilter.set((event.target as HTMLSelectElement).value);
    this.reloadFromFirstPage();
  }

  protected onStartChange(event: Event): void {
    this.startDate.set((event.target as HTMLInputElement).value);
    this.reloadFromFirstPage();
  }

  protected onEndChange(event: Event): void {
    this.endDate.set((event.target as HTMLInputElement).value);
    this.reloadFromFirstPage();
  }

  protected clearFilters(): void {
    this.actionFilter.set('');
    this.startDate.set('');
    this.endDate.set('');
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

  protected absolute(iso: string): string {
    return formatAbsoluteTime(iso);
  }

  protected actionTone(action: string): string {
    if (action.startsWith('USER_DISABLE') || action.includes('DELETE')) {
      return 'bg-danger-soft text-danger';
    }
    if (action === 'PASSWORD_RESET') {
      return 'bg-warning-soft text-warning';
    }
    if (action.startsWith('USER_CREATE') || action.startsWith('MEMBER_ADD')) {
      return 'bg-success-soft text-success';
    }
    return 'bg-slate-100 text-slate-600';
  }

  protected targetText(log: AuditLogOut): string {
    const type = log.target_type === 'USER' ? '用户' : log.target_type === 'PROJECT' ? '项目' : log.target_type;
    return `${type} · ${log.target_name ?? log.target_id ?? '—'}`;
  }

  protected detailText(log: AuditLogOut): string {
    return log.detail ? JSON.stringify(log.detail) : '';
  }
}

/** 本地日期的 00:00:00.000（后端再换 UTC，避免"当天凌晨的记录被时区挤到前一天"）。 */
function startOfDay(value: string): string {
  const [year, month, day] = value.split('-').map(Number);
  return new Date(year, month - 1, day, 0, 0, 0, 0).toISOString();
}

function endOfDay(value: string): string {
  const [year, month, day] = value.split('-').map(Number);
  return new Date(year, month - 1, day, 23, 59, 59, 999).toISOString();
}
