/** 与 prd-07 §6.5 / prd-02 §5.1 对齐的图纸与上传模型。 */

import { UserBrief } from './project.model';

export type FileType = 'pdf' | 'dxf';

export type DrawingStatus =
  | 'UPLOADING'
  | 'UPLOADED'
  | 'PARSING'
  | 'PARSED'
  | 'REVIEWING'
  | 'REVIEW_DONE'
  | 'PARSE_FAILED'
  | 'REVIEW_FAILED'
  | 'ARCHIVED';

export type SeverityKey = 'BLOCKER' | 'MAJOR' | 'MINOR' | 'INFO';

export interface DrawingOut {
  id: string;
  project_id: string;
  name: string;
  file_name: string;
  file_type: FileType;
  file_size: number;
  version: number;
  parent_drawing_id: string | null;
  is_current: boolean;
  status: DrawingStatus;
  status_text: string;
  page_count: number | null;
  /** {"BLOCKER":2,"MAJOR":5,"MINOR":8,"INFO":1}，未预审为 null */
  issue_stat: Partial<Record<SeverityKey, number>> | null;
  parse_error: string | null;
  uploaded_by: UserBrief;
  created_at: string;
  updated_at: string;
}

export interface UploadSessionCreateRequest {
  project_id: string;
  file_name: string;
  file_type: FileType;
  total_size: number;
  md5?: string;
}

export interface UploadSessionOut {
  upload_id: string;
  chunk_size: number;
  chunk_total: number;
  expires_at: string;
}

export interface ChunkUploadResponse {
  upload_id: string;
  received: number;
  chunk_total: number;
}

export interface ViewUrlResponse {
  url: string;
  expires_at: string;
  file_type: FileType;
}

export interface ReviewTaskOut {
  id: string;
  drawing_id: string;
  status: string;
  status_text: string;
  progress: number;
  issue_total: number;
  created_at: string;
}
