import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { forkJoin } from 'rxjs';
import { finalize } from 'rxjs/operators';
import {
  GlobalRole,
  PermissionGroupOut,
  PermissionOut,
  RoleOut,
} from '../../core/models/admin.model';
import { AdminService } from '../../core/services/admin.service';
import { IconComponent } from '../../shared/icon/icon';

interface PermissionGroup {
  readonly module: string;
  /** 分组标题用后端下发的 module_text，前端不再维护第二套模块字典 */
  readonly label: string;
  readonly items: readonly PermissionOut[];
}

/** 列顺序固定：矩阵是给人读的，别让后端返回顺序决定视线 */
const ROLE_ORDER: readonly GlobalRole[] = ['ADMIN', 'REVIEWER', 'DESIGNER', 'VIEWER'];

/** 角色与权限页（prd-05 §7）：只读矩阵，单元格不可点。 */
@Component({
  selector: 'app-role-permissions',
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './role-permissions.html',
})
export class RolePermissionsComponent {
  private readonly admin = inject(AdminService);

  protected readonly roles = signal<readonly RoleOut[]>([]);
  protected readonly loading = signal(true);
  protected readonly loadError = signal(false);
  protected readonly selectedRole = signal<GlobalRole>('ADMIN');
  private readonly collapsedGroups = signal<ReadonlySet<string>>(new Set());
  private readonly permissionGroups = signal<readonly PermissionGroupOut[]>([]);

  protected readonly orderedRoles = computed<readonly RoleOut[]>(() => {
    const byCode = new Map(this.roles().map((role) => [role.code, role]));
    return ROLE_ORDER.flatMap((code) => {
      const role = byCode.get(code);
      return role ? [role] : [];
    });
  });

  protected readonly groups = computed<readonly PermissionGroup[]>(() =>
    this.permissionGroups().map((group) => ({
      module: group.module,
      label: group.module_text,
      items: group.permissions,
    })),
  );

  protected readonly selected = computed(
    () => this.orderedRoles().find((role) => role.code === this.selectedRole()) ?? null,
  );

  protected readonly totalPermissions = computed(() =>
    this.permissionGroups().reduce((sum, group) => sum + group.permissions.length, 0),
  );

  constructor() {
    this.load();
  }

  protected load(): void {
    this.loading.set(true);
    this.loadError.set(false);
    forkJoin({
      roles: this.admin.listRoles(),
      groups: this.admin.listPermissions(),
    })
      .pipe(finalize(() => this.loading.set(false)))
      .subscribe({
        next: ({ roles, groups }) => {
          this.roles.set(roles);
          this.permissionGroups.set(groups);
          if (!roles.some((role) => role.code === this.selectedRole())) {
            this.selectedRole.set(roles[0]?.code ?? 'ADMIN');
          }
        },
        error: () => this.loadError.set(true),
      });
  }

  protected selectRole(code: GlobalRole): void {
    this.selectedRole.set(code);
  }

  protected isExpanded(module: string): boolean {
    return !this.collapsedGroups().has(module);
  }

  protected toggleGroup(module: string): void {
    this.collapsedGroups.update((set) => {
      const next = new Set(set);
      if (next.has(module)) {
        next.delete(module);
      } else {
        next.add(module);
      }
      return next;
    });
  }

  /** 只读：只负责查表，不提供任何写入入口（单元格不是按钮）。 */
  protected has(role: RoleOut, code: string): boolean {
    return role.permissions.includes(code);
  }

  protected rolePermissionCount(role: RoleOut): string {
    return `${role.permissions.length}/${this.totalPermissions()}`;
  }
}
