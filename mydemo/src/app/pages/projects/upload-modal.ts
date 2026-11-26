import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  OnDestroy,
  OnInit,
  computed,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { Subscription, finalize, switchMap } from 'rxjs';
import { readErrorMessage } from '../../core/api-error';
import { FILE_TYPE_BADGE, FILE_TYPE_LABEL } from '../../core/dictionaries';
import { DrawingOut, FileType } from '../../core/models/drawing.model';
import { ToastService } from '../../core/services/toast.service';
import { UploadProgress, UploadService } from '../../core/services/upload.service';
import { inspectFile } from '../../core/upload-rules';
import { formatFileSize, formatSpeed } from '../../shared/bytes';
import { IconComponent } from '../../shared/icon/icon';
import { DwgGuideModalComponent } from './dwg-guide-modal';

type ItemStatus = 'queued' | 'creating' | 'uploading' | 'merging' | 'done' | 'error' | 'rejected';

interface UploadItem {
  readonly id: string;
  readonly file: File;
  readonly fileName: string;
  readonly fileType: FileType | null;
  readonly size: number;
  readonly status: ItemStatus;
  readonly progress: number;
  readonly uploadedChunks: number;
  readonly chunkTotal: number;
  readonly speedBps: number;
  readonly error: string | null;
  readonly drawingId: string | null;
}

const WORKING_STATUSES: ReadonlySet<ItemStatus> = new Set<ItemStatus>([
  'queued',
  'creating',
  'uploading',
  'merging',
]);

/**
 * 上传弹窗：拖拽 → 前端校验 → 建会话 → 5MB 分片、3 并发 → 合并。
 *
 * 队列串行（一次只传一个文件），文件内分片并发；进度按字节聚合上报。
 * 关闭弹窗 / 组件销毁都会退订并 abort 在飞请求，不留后台流量。
 */
@Component({
  selector: 'app-upload-modal',
  imports: [IconComponent, DwgGuideModalComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:keydown.escape)': 'requestClose()',
  },
  template: `
    <div
      class="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto bg-slate-950/40 px-4 py-8 backdrop-blur-sm sm:items-center"
      (click)="requestClose()"
      (dragover)="$event.preventDefault()"
      (drop)="$event.preventDefault()"
    >
      <div
        class="w-full max-w-3xl animate-fade-in rounded-card bg-white shadow-float"
        role="dialog"
        aria-modal="true"
        aria-labelledby="upload-modal-title"
        (click)="$event.stopPropagation()"
      >
        <header class="flex items-start justify-between gap-4 border-b border-slate-100 px-6 py-5">
          <div>
            <h2 id="upload-modal-title" class="text-base font-semibold text-slate-900">上传图纸</h2>
            <p class="mt-1 text-xs text-slate-400">
              支持 PDF / DXF · 5MB 分片、3 路并发 · 关闭页面前会提示
            </p>
          </div>
          <button type="button" class="icon-btn -mr-2 -mt-1" aria-label="关闭" (click)="requestClose()">
            <app-icon name="close" [size]="16" />
          </button>
        </header>

        <div class="px-6 py-5">
          <div
            class="rounded-card border border-dashed px-6 py-9 text-center transition-colors"
            [class.border-primary]="dragging()"
            [class.bg-primary-soft]="dragging()"
            [class.border-slate-200]="!dragging()"
            (dragenter)="onDragEnter($event)"
            (dragover)="onDragOver($event)"
            (dragleave)="onDragLeave($event)"
            (drop)="onDrop($event)"
          >
            <span
              class="mx-auto flex h-11 w-11 items-center justify-center rounded-full bg-slate-50 text-slate-400 transition-colors"
              [class.bg-white]="dragging()"
              [class.text-primary]="dragging()"
            >
              <app-icon name="upload" [size]="20" />
            </span>
            <p class="mt-3 text-sm text-slate-600">
              把 PDF / DXF 拖到这里，或
              <button
                type="button"
                class="font-medium text-primary transition-colors hover:text-primary-hover"
                (click)="openFilePicker()"
              >
                选择文件
              </button>
            </p>
            <p class="mt-1.5 text-xs text-slate-400">PDF ≤ 100MB · DXF ≤ 50MB · DWG 需先转换为 DXF</p>
          </div>

          @if (items().length > 0) {
            <ul class="mt-5 divide-y divide-slate-100 rounded-card border border-slate-100">
              @for (item of items(); track item.id) {
                <li class="px-4 py-3.5">
                  <div class="flex items-start gap-3">
                    <span
                      class="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-slate-50 text-slate-400"
                      [class.bg-danger-soft]="item.status === 'rejected'"
                      [class.text-danger]="item.status === 'rejected'"
                    >
                      <app-icon
                        [name]="item.status === 'rejected' ? 'rejected' : item.status === 'done' ? 'check' : 'file'"
                        [size]="16"
                      />
                    </span>

                    <div class="min-w-0 flex-1">
                      <div class="flex items-start justify-between gap-3">
                        <div class="min-w-0">
                          <p class="truncate text-sm font-medium text-slate-900">{{ item.fileName }}</p>
                          <p class="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-xs text-slate-400">
                            @if (item.fileType) {
                              <span
                                class="rounded px-1 py-0.5 text-[10px] font-medium"
                                [class]="badgeClass(item.fileType)"
                              >
                                {{ typeLabel(item.fileType) }}
                              </span>
                            }
                            <span>{{ sizeText(item.size) }}</span>
                            @if (item.speedBps > 0) {
                              <span>· {{ speedText(item.speedBps) }}</span>
                            }
                            <span>· {{ statusText(item) }}</span>
                          </p>
                        </div>

                        <button
                          type="button"
                          class="shrink-0 rounded-md px-2 py-1 text-xs font-medium transition-colors hover:bg-slate-100"
                          [class.text-primary]="item.status === 'error'"
                          [class.text-slate-400]="item.status !== 'error'"
                          (click)="item.status === 'error' ? retry(item) : remove(item)"
                        >
                          {{
                            item.status === 'error'
                              ? '重试'
                              : item.status === 'done' || item.status === 'rejected'
                                ? '移除'
                                : '取消'
                          }}
                        </button>
                      </div>

                      @if (item.status !== 'rejected') {
                        <div class="mt-2 h-1 overflow-hidden rounded-full bg-slate-100">
                          <div
                            class="h-full rounded-full transition-[width] duration-200 ease-out"
                            [class.bg-primary]="item.status !== 'done' && item.status !== 'error'"
                            [class.bg-success]="item.status === 'done'"
                            [class.bg-danger]="item.status === 'error'"
                            [style.width.%]="item.status === 'done' ? 100 : item.progress * 100"
                          ></div>
                        </div>
                      }

                      @if (item.status === 'merging') {
                        <p class="mt-1.5 text-xs text-slate-400">
                          分片已全部送达，正在服务端合并并校验…
                        </p>
                      }
                      @if (item.error) {
                        <p class="mt-1.5 text-xs text-danger">{{ item.error }}</p>
                      }
                    </div>
                  </div>
                </li>
              }
            </ul>
          }

          @if (confirmingClose()) {
            <div class="mt-4 rounded-lg border border-warning/30 bg-warning-soft px-3.5 py-3 text-sm">
              <p class="text-slate-700">
                仍有 {{ activeCount() }} 个文件在上传，关闭会中断并丢弃已有进度。
              </p>
              <div class="mt-2.5 flex gap-2">
                <button type="button" class="btn-secondary h-9 px-3 text-xs" (click)="confirmingClose.set(false)">
                  继续上传
                </button>
                <button type="button" class="btn-danger h-9 px-3 text-xs" (click)="confirmClose()">
                  中断并关闭
                </button>
              </div>
            </div>
          }
        </div>

        <footer class="flex items-center justify-between gap-3 border-t border-slate-100 px-6 py-4">
          <p class="text-xs text-slate-400">
            @if (items().length === 0) {
              选择文件后开始上传
            } @else {
              已完成 {{ completedCount() }} / {{ items().length }} 个文件
            }
          </p>
          <button type="button" class="btn-secondary" (click)="requestClose()">关闭</button>
        </footer>
      </div>
    </div>

    <input
      #fileInput
      type="file"
      class="hidden"
      multiple
      accept=".pdf,.dxf,application/pdf,image/vnd.dxf"
      (change)="onFilePicked($event)"
    />
    <input #dxfInput type="file" class="hidden" accept=".dxf" (change)="onDxfPicked($event)" />

    @if (dwgPrompt(); as name) {
      <app-dwg-guide-modal
        [fileName]="name"
        (dismissed)="dwgPrompt.set(null)"
        (chooseFile)="chooseDxf()"
      />
    }
  `,
})
export class UploadModalComponent implements OnInit, OnDestroy {
  private readonly uploadService = inject(UploadService);
  private readonly toast = inject(ToastService);

  readonly projectId = input.required<string>();
  readonly closed = output<void>();
  readonly uploaded = output<DrawingOut>();

  protected readonly items = signal<readonly UploadItem[]>([]);
  protected readonly dragging = signal(false);
  protected readonly confirmingClose = signal(false);
  protected readonly dwgPrompt = signal<string | null>(null);

  protected readonly hasWork = computed(() =>
    this.items().some((item) => WORKING_STATUSES.has(item.status)),
  );
  protected readonly activeCount = computed(
    () => this.items().filter((item) => WORKING_STATUSES.has(item.status)).length,
  );
  protected readonly completedCount = computed(
    () => this.items().filter((item) => item.status === 'done').length,
  );

  private readonly fileInput = viewChild<ElementRef<HTMLInputElement>>('fileInput');
  private readonly dxfInput = viewChild<ElementRef<HTMLInputElement>>('dxfInput');

  private readonly subscriptions = new Map<string, Subscription>();
  private readonly lastTick = new Map<string, { time: number; bytes: number }>();
  private readonly dragDepth = signal(0);
  private processing = false;

  private readonly beforeUnload = (event: BeforeUnloadEvent): void => {
    if (!this.hasWork()) {
      return;
    }
    event.preventDefault();
    event.returnValue = '';
  };

  ngOnInit(): void {
    window.addEventListener('beforeunload', this.beforeUnload);
    document.body.style.overflow = 'hidden';
  }

  ngOnDestroy(): void {
    window.removeEventListener('beforeunload', this.beforeUnload);
    document.body.style.overflow = '';
    this.abortAll();
  }

  /* ---------- 拖拽 ---------- */

  protected onDragEnter(event: DragEvent): void {
    event.preventDefault();
    event.stopPropagation();
    this.dragDepth.update((depth) => depth + 1);
    this.dragging.set(true);
  }

  protected onDragOver(event: DragEvent): void {
    event.preventDefault();
    event.stopPropagation();
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = 'copy';
    }
  }

  protected onDragLeave(event: DragEvent): void {
    event.preventDefault();
    event.stopPropagation();
    const next = Math.max(0, this.dragDepth() - 1);
    this.dragDepth.set(next);
    this.dragging.set(next > 0);
  }

  protected onDrop(event: DragEvent): void {
    event.preventDefault();
    event.stopPropagation();
    this.dragDepth.set(0);
    this.dragging.set(false);
    this.enqueue(Array.from(event.dataTransfer?.files ?? []));
  }

  /* ---------- 文件选择 ---------- */

  protected openFilePicker(): void {
    this.fileInput()?.nativeElement.click();
  }

  protected chooseDxf(): void {
    this.dxfInput()?.nativeElement.click();
  }

  protected onFilePicked(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.enqueue(Array.from(input.files ?? []));
    input.value = '';
  }

  protected onDxfPicked(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.enqueue(Array.from(input.files ?? []));
    input.value = '';
    this.dwgPrompt.set(null);
  }

  /* ---------- 队列 ---------- */

  private enqueue(files: readonly File[]): void {
    if (files.length === 0) {
      return;
    }

    const additions: UploadItem[] = [];
    let dwgName: string | null = null;

    for (const file of files) {
      const inspection = inspectFile(file);

      if (inspection.kind === 'dwg') {
        // 命中 DWG：不建会话、不进队列，只弹引导
        dwgName = inspection.fileName;
        continue;
      }

      if (inspection.kind === 'rejected') {
        additions.push(this.makeItem(file, null, file.name, 'rejected', inspection.message));
        continue;
      }

      if (inspection.renamed) {
        this.toast.show(`文件名含非法字符，已替换为「${inspection.fileName}」`);
      }
      additions.push(
        this.makeItem(file, inspection.fileType, inspection.fileName, 'queued', null),
      );
    }

    if (additions.length > 0) {
      this.items.update((items) => [...items, ...additions]);
    }
    if (dwgName) {
      this.dwgPrompt.set(dwgName);
    }
    this.processQueue();
  }

  private makeItem(
    file: File,
    fileType: FileType | null,
    fileName: string,
    status: ItemStatus,
    error: string | null,
  ): UploadItem {
    return {
      id: crypto.randomUUID(),
      file,
      fileName,
      fileType,
      size: file.size,
      status,
      progress: 0,
      uploadedChunks: 0,
      chunkTotal: 0,
      speedBps: 0,
      error,
      drawingId: null,
    };
  }

  /** 队列串行：一次只传一个文件，避免多文件争抢带宽（prd-02 §4.3）。 */
  private processQueue(): void {
    if (this.processing) {
      return;
    }
    const next = this.items().find((item) => item.status === 'queued');
    if (!next) {
      return;
    }
    this.processing = true;
    this.startItem(next);
  }

  private startItem(item: UploadItem): void {
    this.patch(item.id, { status: 'creating', progress: 0, error: null, speedBps: 0 });

    const subscription = this.uploadService
      .createSession({
        project_id: this.projectId(),
        file_name: item.fileName,
        file_type: item.fileType ?? 'pdf',
        total_size: item.size,
      })
      .pipe(
        switchMap((session) => {
          this.patch(item.id, { status: 'uploading', chunkTotal: session.chunk_total });
          return this.uploadService.upload(item.file, session, {
            onProgress: (progress) => this.onProgress(item.id, progress),
          });
        }),
        finalize(() => {
          this.subscriptions.delete(item.id);
          this.lastTick.delete(item.id);
          this.processing = false;
          this.processQueue();
        }),
      )
      .subscribe({
        next: (drawing) => {
          this.patch(item.id, {
            status: 'done',
            progress: 1,
            speedBps: 0,
            drawingId: drawing.id,
          });
          this.toast.success(`「${drawing.name}」上传完成`);
          this.uploaded.emit(drawing);
        },
        error: (error: unknown) => {
          this.patch(item.id, {
            status: 'error',
            speedBps: 0,
            error: readErrorMessage(error, '上传失败，请重试'),
          });
        },
      });

    this.subscriptions.set(item.id, subscription);
  }

  protected retry(item: UploadItem): void {
    this.patch(item.id, { status: 'queued', error: null, progress: 0, uploadedChunks: 0 });
    this.processQueue();
  }

  protected remove(item: UploadItem): void {
    this.subscriptions.get(item.id)?.unsubscribe();
    this.subscriptions.delete(item.id);
    this.lastTick.delete(item.id);
    this.items.update((items) => items.filter((entry) => entry.id !== item.id));
    this.processing = false;
    this.processQueue();
  }

  /* ---------- 进度 ---------- */

  private onProgress(id: string, progress: UploadProgress): void {
    const item = this.items().find((entry) => entry.id === id);
    if (!item) {
      return;
    }

    const now = performance.now();
    const last = this.lastTick.get(id);
    let speed = item.speedBps;

    if (last) {
      const elapsed = (now - last.time) / 1000;
      const deltaBytes = progress.sentBytes - last.bytes;
      if (elapsed > 0.05 && deltaBytes > 0) {
        const instant = deltaBytes / elapsed;
        // EMA 平滑，避免进度条旁的速度数字乱跳
        speed = speed > 0 ? speed * 0.7 + instant * 0.3 : instant;
      }
    }
    this.lastTick.set(id, { time: now, bytes: progress.sentBytes });

    const allChunksSent =
      progress.chunkTotal > 0 && progress.uploadedChunks >= progress.chunkTotal;

    this.patch(id, {
      progress: progress.totalBytes > 0 ? progress.sentBytes / progress.totalBytes : 0,
      uploadedChunks: progress.uploadedChunks,
      chunkTotal: progress.chunkTotal,
      speedBps: speed,
      status: allChunksSent ? 'merging' : 'uploading',
    });
  }

  /* ---------- 关闭 ---------- */

  protected requestClose(): void {
    if (this.hasWork()) {
      this.confirmingClose.set(true);
      return;
    }
    this.closed.emit();
  }

  protected confirmClose(): void {
    this.abortAll();
    this.closed.emit();
  }

  private abortAll(): void {
    for (const subscription of this.subscriptions.values()) {
      subscription.unsubscribe();
    }
    this.subscriptions.clear();
    this.lastTick.clear();
  }

  /* ---------- 展示辅助 ---------- */

  private patch(id: string, changes: Partial<Omit<UploadItem, 'id' | 'file'>>): void {
    this.items.update((items) =>
      items.map((item) => (item.id === id ? { ...item, ...changes } : item)),
    );
  }

  protected typeLabel(fileType: FileType): string {
    return FILE_TYPE_LABEL[fileType];
  }

  protected badgeClass(fileType: FileType): string {
    return FILE_TYPE_BADGE[fileType];
  }

  protected sizeText(bytes: number): string {
    return formatFileSize(bytes);
  }

  protected speedText(bytesPerSecond: number): string {
    return formatSpeed(bytesPerSecond);
  }

  protected statusText(item: UploadItem): string {
    switch (item.status) {
      case 'queued':
        return '排队中';
      case 'creating':
        return '正在创建上传会话';
      case 'uploading':
        return `${item.uploadedChunks}/${item.chunkTotal || '—'} 片 · ${Math.round(item.progress * 100)}%`;
      case 'merging':
        return '正在合并分片';
      case 'done':
        return '已完成';
      case 'error':
        return '上传失败';
      case 'rejected':
        return '已拦截';
    }
  }
}
