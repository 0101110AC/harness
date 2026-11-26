import type { PageViewport } from 'pdfjs-dist';
import type { Location } from '../models/review.model';

/** 屏幕像素矩形：相对页面容器左上角，单位 CSS px（不含 devicePixelRatio） */
export interface ScreenRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

/**
 * PDF 坐标 ↔ 屏幕坐标的唯一换算入口。
 *
 * 三套坐标在这里交汇：
 * - `Location`：落库用的 PDF 用户空间 pt，`origin` 决定 y 轴朝上还是朝下；
 * - `PageViewport`：PDF.js 的坐标变换，已经吃掉了 `scale`、`rotation`、`offset`；
 * - 屏幕像素：页面容器的 CSS px（高 DPI 由渲染层的 backing store 处理，与这里无关）。
 *
 * 关键点：旋转 90/180/270 时 bbox 不再是"左上角 + 宽高"，必须把四角都过一遍
 * `convertToViewportPoint` 再取外接框，否则批注框会横竖颠倒。
 */

function viewDims(viewport: PageViewport): { x: number; y: number; w: number; h: number } {
  const [x0, y0, x1, y1] = viewport.viewBox;
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

/** PDF 坐标 → 屏幕像素（含缩放、旋转、偏移与版本尺寸归一化） */
export function toScreen(location: Location, viewport: PageViewport): ScreenRect {
  const view = viewDims(viewport);
  const page = location.page_size;
  // 落库时的 page_size 可能与当前图纸版本不同：按比例映射，而不是硬用 pt
  const scaleX = page && page.w > 0 ? view.w / page.w : 1;
  const scaleY = page && page.h > 0 ? view.h / page.h : 1;

  const leftRel = location.bbox.x * scaleX;
  const widthRel = location.bbox.w * scaleX;
  const heightRel = location.bbox.h * scaleY;
  // 统一成"自上而下"的 top 偏移，再翻回 PDF 用户空间交给 viewport
  const topRel =
    location.origin === 'bottom-left'
      ? view.h - (location.bbox.y + location.bbox.h) * scaleY
      : location.bbox.y * scaleY;

  const pdfTop = view.y + view.h - topRel;
  const pdfBottom = pdfTop - heightRel;

  const corners = [
    viewport.convertToViewportPoint(view.x + leftRel, pdfTop),
    viewport.convertToViewportPoint(view.x + leftRel + widthRel, pdfTop),
    viewport.convertToViewportPoint(view.x + leftRel, pdfBottom),
    viewport.convertToViewportPoint(view.x + leftRel + widthRel, pdfBottom),
  ];

  const xs = corners.map((point) => point[0]);
  const ys = corners.map((point) => point[1]);
  const minX = Math.min(...xs);
  const minY = Math.min(...ys);
  return {
    left: minX,
    top: minY,
    width: Math.max(...xs) - minX,
    height: Math.max(...ys) - minY,
  };
}

/** 屏幕像素 → 落库坐标（prd-07 §4：pdf 为 pt + top-left，dxf 为 mm + bottom-left） */
export function toLocation(
  rect: ScreenRect,
  viewport: PageViewport,
  options: { pageNo: number; fileType: 'pdf' | 'dxf' },
): Location {
  const view = viewDims(viewport);
  const corners = [
    viewport.convertToPdfPoint(rect.left, rect.top),
    viewport.convertToPdfPoint(rect.left + rect.width, rect.top),
    viewport.convertToPdfPoint(rect.left, rect.top + rect.height),
    viewport.convertToPdfPoint(rect.left + rect.width, rect.top + rect.height),
  ];

  const xs = corners.map((point) => point[0] - view.x);
  const ys = corners.map((point) => point[1] - view.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);

  const width = maxX - minX;
  const height = maxY - minY;
  const isDxf = options.fileType === 'dxf';
  return {
    file_type: options.fileType,
    page_no: options.pageNo,
    bbox: {
      x: round1(minX),
      y: round1(isDxf ? minY : view.h - maxY),
      w: round1(width),
      h: round1(height),
    },
    unit: isDxf ? 'mm' : 'pt',
    coord_system: isDxf ? 'dxf_wcs' : 'pdf_user_space',
    origin: isDxf ? 'bottom-left' : 'top-left',
    page_size: { w: round1(view.w), h: round1(view.h) },
    dxf: null,
  };
}

function round1(value: number): number {
  return Math.round(value * 10) / 10;
}
