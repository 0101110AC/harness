import { HttpClient, HttpEvent, HttpEventType, HttpResponse } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import {
  concatMap,
  filter,
  finalize,
  from,
  map,
  mergeMap,
  Observable,
  retry,
  tap,
  timer,
  toArray,
} from 'rxjs';
import { API_BASE_URL } from '../api';
import {
  ChunkUploadResponse,
  DrawingOut,
  UploadSessionCreateRequest,
  UploadSessionOut,
} from '../models/drawing.model';

/** 页面可直接绑定的进度快照（字节口径，百分比由 UI 自行换算）。 */
export interface UploadProgress {
  readonly sentBytes: number;
  readonly totalBytes: number;
  readonly uploadedChunks: number;
  readonly chunkTotal: number;
  /** 当前在飞的分片数，用于自检并发是否真的被限制住 */
  readonly inFlight: number;
}

export interface UploadOptions {
  readonly concurrency?: number;
  readonly onProgress?: (progress: UploadProgress) => void;
}

/** 并发数上限：3 片同时发，兼顾带宽与浏览器主线程（规范要求，不随文件大小变化）。 */
export const DEFAULT_UPLOAD_CONCURRENCY = 3;
/** 单片失败重试：3 次，间隔 1s / 2s / 4s（prd-02 §4.3）。 */
const CHUNK_RETRY_COUNT = 3;
const CHUNK_RETRY_DELAYS = [1000, 2000, 4000];

/**
 * 分片上传：建会话 → 切片并发 PUT → 合并。
 *
 * 只暴露一个 `upload()`，进度通过回调上报；返回的 Observable 完成即拿到 DrawingOut。
 * 取消 = 退订：HttpClient 会 abort 在飞请求，不会留下后台流量。
 */
@Injectable({ providedIn: 'root' })
export class UploadService {
  private readonly http = inject(HttpClient);

  createSession(payload: UploadSessionCreateRequest): Observable<UploadSessionOut> {
    return this.http.post<UploadSessionOut>(`${API_BASE_URL}/drawings/upload-sessions`, payload);
  }

  completeSession(uploadId: string): Observable<DrawingOut> {
    return this.http.post<DrawingOut>(
      `${API_BASE_URL}/drawings/upload-sessions/${uploadId}/complete`,
      {},
    );
  }

  upload(file: File, session: UploadSessionOut, options: UploadOptions = {}): Observable<DrawingOut> {
    const concurrency = Math.max(1, options.concurrency ?? DEFAULT_UPLOAD_CONCURRENCY);
    const chunkSize = session.chunk_size > 0 ? session.chunk_size : 5 * 1024 * 1024;
    const indexes = Array.from({ length: session.chunk_total }, (_unused, index) => index);

    const sent = new Map<number, number>();
    const done = new Set<number>();
    let inFlight = 0;

    const emit = (): void => {
      let sentBytes = 0;
      for (const value of sent.values()) {
        sentBytes += value;
      }
      options.onProgress?.({
        sentBytes,
        totalBytes: file.size,
        uploadedChunks: done.size,
        chunkTotal: session.chunk_total,
        inFlight,
      });
    };

    return from(indexes).pipe(
      // mergeMap 的第二个参数就是"并发上限"，天然实现 3 个分片同时发
      mergeMap((index) => {
        const start = index * chunkSize;
        const chunk = file.slice(start, Math.min(start + chunkSize, file.size));

        sent.set(index, 0);
        inFlight += 1;
        emit();

        return this.putChunk(session.upload_id, index, chunk, (loaded) => {
          sent.set(index, Math.min(loaded, chunk.size));
          emit();
        }).pipe(
          finalize(() => {
            inFlight -= 1;
          }),
          tap(() => {
            done.add(index);
            sent.set(index, chunk.size);
            emit();
          }),
          map(() => void 0),
        );
      }, concurrency),
      // 关键：每个分片完成都会 emit 一个值，concatMap 会"对第一个值"就触发合并。
      // 用 toArray 把上游压成一次 emit（只在源 complete 时发生），确保全部分片送达后才合并。
      toArray(),
      concatMap(() => this.completeSession(session.upload_id)),
    );
  }

  private putChunk(
    uploadId: string,
    index: number,
    chunk: Blob,
    onLoaded: (loadedBytes: number) => void,
  ): Observable<ChunkUploadResponse> {
    return this.http
      .put<ChunkUploadResponse>(
        `${API_BASE_URL}/drawings/upload-sessions/${uploadId}/chunks/${index}`,
        chunk,
        {
          observe: 'events',
          reportProgress: true,
          headers: { 'Content-Type': 'application/octet-stream' },
        },
      )
      .pipe(
        tap((event: HttpEvent<ChunkUploadResponse>) => {
          if (event.type === HttpEventType.UploadProgress) {
            onLoaded(event.loaded);
          }
        }),
        filter(
          (event): event is HttpResponse<ChunkUploadResponse> =>
            event.type === HttpEventType.Response,
        ),
        map((event) => event.body as ChunkUploadResponse),
        retry({
          count: CHUNK_RETRY_COUNT,
          delay: (_error, attempt) =>
            timer(CHUNK_RETRY_DELAYS[Math.min(attempt - 1, CHUNK_RETRY_DELAYS.length - 1)]),
        }),
      );
  }
}
