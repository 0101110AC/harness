import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  effect,
  inject,
  input,
  output,
  signal,
  untracked,
  viewChild,
} from '@angular/core';
import {
  GlobalWorkerOptions,
  getDocument,
  type PDFDocumentLoadingTask,
  type PDFDocumentProxy,
  type PageViewport,
  type RenderTask,
} from 'pdfjs-dist';

/**
 * Worker 由 angular.json 的 assets 复制到 `/pdfjs/pdf.worker.min.mjs`。
 *
 * 注意：`new URL('pdfjs-dist/build/pdf.worker.min.mjs', import.meta.url)` 在 Angular 的
 * esbuild 管线里**不会被重写**（只有 `new Worker(new URL(...))` 有专门处理），
 * 打包后会原样保留成相对 chunk 的裸路径 —— 运行期 404、PDF.js 直接报 "fake worker"。
 * 所以这里用"构建期复制 + 运行期按 base href 解析"的组合：子路径部署也不会错。
 */
GlobalWorkerOptions.workerSrc = new URL('pdfjs/pdf.worker.min.mjs', document.baseURI).toString();

/** 单边与总面积上限：400% 看 A1 图纸时 naive 的 dpr×viewport 会直接顶到浏览器画布上限 */
const MAX_BACKING_DIM = 8192;
const MAX_BACKING_AREA = 24_000_000;

export type PdfRenderStatus = 'idle' | 'loading' | 'ready' | 'error';

export interface PdfStatusEvent {
  status: PdfRenderStatus;
  message?: string;
}

function isCancelled(error: unknown): boolean {
  return (
    error instanceof Error &&
    (error.name === 'RenderingCancelledException' || error.name === 'AbortException')
  );
}

/** 高 DPI 也要有上限：超限时宁可降采样，也不能让 canvas 分配失败变白屏 */
function backingScale(cssWidth: number, cssHeight: number): number {
  const device = Math.min(window.devicePixelRatio || 1, 2);
  const byDim = MAX_BACKING_DIM / Math.max(cssWidth, cssHeight, 1);
  const byArea = Math.sqrt(MAX_BACKING_AREA / Math.max(cssWidth * cssHeight, 1));
  return Math.max(0.25, Math.min(device, byDim, byArea));
}

/**
 * 真实 PDF 渲染层：只负责「把某一页画到 canvas」。
 *
 * 对外只暴露 `pageReady`（viewport + 总页数）与渲染状态；
 * 批注叠加层、滚轮缩放、翻页等交互仍归父组件，坐标一律走 `coordinates.ts`，
 * 这样渲染层换实现（PDF.js → 服务端图片）不影响坐标契约。
 */
@Component({
  selector: 'app-pdf-canvas',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'pointer-events-none absolute inset-0 block' },
  template: `<canvas #canvas class="block rounded-sm bg-white"></canvas>`,
})
export class PdfCanvasComponent {
  private readonly destroyRef = inject(DestroyRef);

  /** 短时签名 URL；null 表示当前图纸不是 PDF 或尚未取到地址 */
  readonly url = input<string | null>(null);
  readonly pageNo = input(1);
  /** 100 = PDF 原始尺寸（1pt = 1 CSS px） */
  readonly scale = input(100);
  /** 换值即重新拉取文档（签名 URL 过期 / 用户点重试） */
  readonly reload = input(0);

  readonly pageReady = output<{ viewport: PageViewport; pageCount: number }>();
  readonly statusChange = output<PdfStatusEvent>();

  protected readonly canvasRef = viewChild.required<ElementRef<HTMLCanvasElement>>('canvas');

  private readonly doc = signal<PDFDocumentProxy | null>(null);
  private readonly status = signal<PdfRenderStatus>('idle');

  private loadingTask: PDFDocumentLoadingTask | null = null;
  private renderTask: RenderTask | null = null;
  private loadSeq = 0;
  private renderSeq = 0;

  constructor() {
    effect(() => {
      const url = this.url();
      this.reload();
      untracked(() => void this.load(url));
    });

    effect(() => {
      const doc = this.doc();
      const pageNo = this.pageNo();
      const scale = this.scale();
      if (!doc) {
        return;
      }
      untracked(() => void this.renderPage(doc, pageNo, scale));
    });

    this.destroyRef.onDestroy(() => {
      this.loadSeq += 1;
      this.renderSeq += 1;
      this.cancelRender();
      // v6 起文档没有 destroy()，销毁权属于加载任务：它会一并终止 worker 与网络请求
      void this.loadingTask?.destroy();
      this.loadingTask = null;
    });
  }

  private setStatus(status: PdfRenderStatus, message?: string): void {
    if (this.status() !== status) {
      this.status.set(status);
      this.statusChange.emit({ status, message });
    }
  }

  private async load(url: string | null): Promise<void> {
    const seq = ++this.loadSeq;
    this.renderSeq += 1;
    this.cancelRender();

    this.doc.set(null);
    const previousTask = this.loadingTask;
    this.loadingTask = null;
    if (previousTask) {
      void previousTask.destroy();
    }

    if (!url) {
      this.setStatus('idle');
      return;
    }

    this.setStatus('loading');
    try {
      // cmaps / standard_fonts 由 angular.json 复制到 /pdfjs/ 下；中文图纸的非嵌入字体靠它兜底
      const base = document.baseURI;
      const task = getDocument({
        url,
        cMapUrl: new URL('pdfjs/cmaps/', base).toString(),
        cMapPacked: true,
        standardFontDataUrl: new URL('pdfjs/standard_fonts/', base).toString(),
      });
      const document_ = await task.promise;
      if (seq !== this.loadSeq) {
        void task.destroy();
        return;
      }
      this.loadingTask = task;
      this.doc.set(document_);
    } catch (error) {
      if (seq !== this.loadSeq) {
        return;
      }
      this.setStatus('error', error instanceof Error ? error.message : 'PDF 加载失败');
      console.error('PDF 加载失败', error);
    }
  }

  private async renderPage(doc: PDFDocumentProxy, pageNo: number, scale: number): Promise<void> {
    const seq = ++this.renderSeq;
    this.cancelRender();

    try {
      const page = await doc.getPage(Math.min(Math.max(1, Math.floor(pageNo)), doc.numPages));
      if (seq !== this.renderSeq) {
        return;
      }
      const viewport = page.getViewport({ scale: Math.max(scale, 1) / 100 });
      this.pageReady.emit({ viewport, pageCount: doc.numPages });

      const canvas = this.canvasRef().nativeElement;
      const cssWidth = Math.max(1, Math.round(viewport.width));
      const cssHeight = Math.max(1, Math.round(viewport.height));
      const dpr = backingScale(cssWidth, cssHeight);

      // backing store 用真实像素，CSS 尺寸保持 viewport 尺寸：批注层永远按 CSS px 对齐
      canvas.width = Math.max(1, Math.round(cssWidth * dpr));
      canvas.height = Math.max(1, Math.round(cssHeight * dpr));
      canvas.style.width = `${cssWidth}px`;
      canvas.style.height = `${cssHeight}px`;

      const task = page.render({
        canvas,
        viewport,
        transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined,
      });
      this.renderTask = task;
      await task.promise;
      if (seq !== this.renderSeq) {
        return;
      }
      this.renderTask = null;
      page.cleanup();
      this.setStatus('ready');
    } catch (error) {
      if (seq !== this.renderSeq || isCancelled(error)) {
        return;
      }
      console.error('PDF 渲染失败', error);
      this.setStatus('error', error instanceof Error ? error.message : 'PDF 渲染失败');
    }
  }

  private cancelRender(): void {
    if (this.renderTask) {
      try {
        this.renderTask.cancel();
      } catch {
        // 已结束的渲染任务再取消会抛，忽略
      }
      this.renderTask = null;
    }
  }
}
