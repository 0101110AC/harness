import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { IconComponent } from '../../shared/icon/icon';

/**
 * DWG 引导弹窗（prd-02 §4.2）。
 *
 * 关键约束：命中 .dwg 时不发起任何上传请求，只做引导——把「不支持」翻译成
 * 「怎么转换成我们支持的格式」，这是对 R2 风险的对冲。
 */
@Component({
  selector: 'app-dwg-guide-modal',
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:keydown.escape)': 'dismissed.emit()',
  },
  template: `
    <div
      class="fixed inset-0 z-[80] flex items-center justify-center bg-slate-950/50 px-4 py-10 backdrop-blur-sm"
      (click)="dismissed.emit()"
    >
      <div
        class="w-full max-w-lg animate-fade-in rounded-card bg-white p-7 shadow-float"
        role="dialog"
        aria-modal="true"
        aria-labelledby="dwg-guide-title"
        (click)="$event.stopPropagation()"
      >
        <div class="flex items-start gap-3">
          <span
            class="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-warning-soft text-warning"
          >
            <app-icon name="fileUp" [size]="18" />
          </span>
          <div class="min-w-0 flex-1">
            <h2 id="dwg-guide-title" class="text-base font-semibold tracking-tight text-slate-900">
              暂不支持 DWG 格式
            </h2>
            <p class="mt-1.5 text-sm leading-6 text-slate-500">
              当前版本仅支持 PDF 与 DXF。DWG 请按以下 3 步转换后上传：
            </p>
          </div>
          <button type="button" class="icon-btn -mr-2 -mt-1.5" aria-label="关闭" (click)="dismissed.emit()">
            <app-icon name="close" [size]="16" />
          </button>
        </div>

        @if (fileName(); as name) {
          <p class="mt-4 truncate rounded-lg bg-slate-50 px-3 py-2 font-mono text-xs text-slate-500">
            已拦截：{{ name }}
          </p>
        }

        <ol class="mt-5 space-y-4">
          <li class="flex gap-3">
            <span
              class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-100 text-[11px] font-semibold text-slate-600"
            >
              1
            </span>
            <p class="text-sm leading-6 text-slate-600">
              用 AutoCAD 打开图纸，选择「另存为」，格式选 <code class="rounded bg-slate-100 px-1 py-0.5 font-mono text-xs text-slate-700">DXF (*.dxf)</code>，版本选 2018。
            </p>
          </li>
          <li class="flex gap-3">
            <span
              class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-100 text-[11px] font-semibold text-slate-600"
            >
              2
            </span>
            <p class="text-sm leading-6 text-slate-600">
              若提示「仅导出选定对象」，请选择「整个图形」。
            </p>
          </li>
          <li class="flex gap-3">
            <span
              class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-100 text-[11px] font-semibold text-slate-600"
            >
              3
            </span>
            <p class="text-sm leading-6 text-slate-600">回到本页，重新上传转换后的 .dxf 文件。</p>
          </li>
        </ol>

        <p class="mt-5 rounded-lg border border-slate-100 bg-slate-50/60 px-3.5 py-3 text-xs leading-5 text-slate-500">
          附注：转换后请确认墙体 / 门窗图层名称未被改动，否则可能影响识别准确率。
        </p>

        <footer class="mt-6 flex justify-end gap-2">
          <button type="button" class="btn-secondary" (click)="dismissed.emit()">我已了解</button>
          <button type="button" class="btn-primary" (click)="chooseFile.emit()">
            <app-icon name="upload" [size]="16" />
            选择 .dxf 文件
          </button>
        </footer>
      </div>
    </div>
  `,
})
export class DwgGuideModalComponent {
  readonly fileName = input<string | null>(null);

  readonly dismissed = output<void>();
  /** 交给上传弹窗去触发受 .dxf 限制的文件选择器，避免这里再持有一个 input */
  readonly chooseFile = output<void>();
}
