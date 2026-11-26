import { FileType } from './models/drawing.model';

/** prd-02 §4.1：体积上限与格式白名单，前端第一道校验（后端仍会复查 MIME 与魔数）。 */
export const PDF_MAX_BYTES = 100 * 1024 * 1024;
export const DXF_MAX_BYTES = 50 * 1024 * 1024;
export const MAX_FILE_NAME_LENGTH = 200;

const ALLOWED_EXTENSIONS: ReadonlySet<string> = new Set<FileType>(['pdf', 'dxf']);
/** 仅允许中英文、数字、`-_.()` 与空格；其余字符替换为 `_`（不阻断上传，只提示）。 */
const ILLEGAL_NAME_CHARS = /[^0-9A-Za-z\u4e00-\u9fa5\-_.() ]/g;

export type UploadRejectionCode = 'ERR_EXT_NOT_ALLOWED' | 'ERR_FILE_TOO_LARGE' | 'ERR_FILE_CORRUPTED';

export type FileInspection =
  | { readonly kind: 'ok'; readonly fileType: FileType; readonly fileName: string; readonly renamed: boolean }
  /** 命中 DWG：走引导弹窗，不进入上传队列、不发任何请求 */
  | { readonly kind: 'dwg'; readonly fileName: string }
  | { readonly kind: 'rejected'; readonly code: UploadRejectionCode; readonly message: string };

export function extensionOf(fileName: string): string {
  const dot = fileName.lastIndexOf('.');
  return dot >= 0 ? fileName.slice(dot + 1).toLowerCase() : '';
}

export function sanitizeFileName(fileName: string): { fileName: string; renamed: boolean } {
  const replaced = fileName.replace(ILLEGAL_NAME_CHARS, '_');
  const truncated =
    replaced.length > MAX_FILE_NAME_LENGTH ? replaced.slice(0, MAX_FILE_NAME_LENGTH) : replaced;
  return { fileName: truncated, renamed: truncated !== fileName };
}

export function maxBytesFor(fileType: FileType): number {
  return fileType === 'pdf' ? PDF_MAX_BYTES : DXF_MAX_BYTES;
}

export function inspectFile(file: File): FileInspection {
  const ext = extensionOf(file.name);
  const { fileName, renamed } = sanitizeFileName(file.name);

  if (ext === 'dwg') {
    return { kind: 'dwg', fileName };
  }

  if (!ALLOWED_EXTENSIONS.has(ext)) {
    return {
      kind: 'rejected',
      code: 'ERR_EXT_NOT_ALLOWED',
      message: '当前版本仅支持 PDF 与 DXF 文件',
    };
  }

  const fileType = ext as FileType;

  if (file.size === 0) {
    return {
      kind: 'rejected',
      code: 'ERR_FILE_CORRUPTED',
      message: '文件为空或已损坏，请从源文件重新导出后再上传',
    };
  }

  if (file.size > maxBytesFor(fileType)) {
    return {
      kind: 'rejected',
      code: 'ERR_FILE_TOO_LARGE',
      message:
        fileType === 'pdf'
          ? 'PDF 文件不能超过 100MB'
          : 'DXF 文件不能超过 50MB，建议在 CAD 中清理无用图层后重试',
    };
  }

  return { kind: 'ok', fileType, fileName, renamed };
}
