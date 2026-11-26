/** 与 prd-07 §6.4 对齐的项目模型。字段名保持 snake_case，前后端零转换。 */

export type ProjectStatus = 'ACTIVE' | 'ARCHIVED';
export type ProjectRole = 'OWNER' | 'REVIEWER' | 'DESIGNER' | 'VIEWER';
export type DesignStage = 'SCHEME' | 'PRELIMINARY' | 'CONSTRUCTION';

export interface UserBrief {
  id: string;
  name: string;
  email: string;
  avatar_url: string | null;
}

export interface ProjectOut {
  id: string;
  name: string;
  code: string | null;
  description: string | null;
  building_type: string | null;
  design_stage: string | null;
  status: ProjectStatus;
  owner: UserBrief;
  drawing_count: number;
  open_annotation_count: number;
  my_project_role: ProjectRole | null;
  created_at: string;
  updated_at: string;
}

export interface ProjectCreateRequest {
  name: string;
  building_type: string | null;
  design_stage: DesignStage | null;
}

/** PATCH /projects/{id}：只发要改的字段（prd-07 §6.4）。 */
export interface ProjectUpdateRequest {
  name?: string;
  description?: string | null;
  building_type?: string | null;
  design_stage?: string | null;
  status?: ProjectStatus;
}

export interface ProjectMemberOut {
  user: UserBrief;
  project_role: ProjectRole;
  open_annotation_count: number;
  joined_at: string;
}

/** 详情比列表多成员列表与项目级统计（prd-07 §6.4）。 */
export interface ProjectDetailOut extends ProjectOut {
  members: ProjectMemberOut[];
}

/** 统一分页信封（prd-07 §1.2）。 */
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}
