import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import {
  Archive,
  ArrowLeft,
  ArrowRight,
  Bell,
  Building2,
  CalendarDays,
  CheckCheck,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  CircleAlert,
  CircleCheck,
  CircleX,
  Clock,
  Crosshair,
  Download,
  Ellipsis,
  Eye,
  FileText,
  FileUp,
  FolderKanban,
  FolderPlus,
  Hand,
  Inbox,
  Info,
  Layers,
  LayoutDashboard,
  ListFilter,
  LoaderCircle,
  Lock,
  LogOut,
  LucideAngularModule,
  LucideIconData,
  Mail,
  MapPin,
  Maximize2,
  Megaphone,
  MessageSquareText,
  MousePointer2,
  PanelLeftClose,
  PanelLeftOpen,
  Paperclip,
  Pause,
  Pencil,
  Play,
  Plus,
  RotateCw,
  ScanLine,
  Search,
  Send,
  Settings,
  ShieldCheck,
  Sparkles,
  Square,
  Trash2,
  Type,
  Undo2,
  Upload,
  User,
  Users,
  X,
  ZoomIn,
  ZoomOut,
} from 'lucide-angular';

/**
 * 全站唯一图标入口。
 *
 * 业务代码只写 `name="bell"`，不直接声明 SVG，保证图标风格、
 * 线宽与尺寸统一（极简主义：统一 1.75 线宽、18px 默认尺寸）。
 * 颜色继承自父级文字色（lucide 默认 stroke: currentColor）。
 */
const ICONS = {
  dashboard: LayoutDashboard,
  project: FolderKanban,
  review: ScanLine,
  annotation: MessageSquareText,
  settings: Settings,
  search: Search,
  bell: Bell,
  logout: LogOut,
  user: User,
  chevronDown: ChevronDown,
  chevronRight: ChevronRight,
  chevronUp: ChevronUp,
  collapse: PanelLeftClose,
  expand: PanelLeftOpen,
  loader: LoaderCircle,
  alert: CircleAlert,
  check: CircleCheck,
  close: X,
  brand: Building2,
  shield: ShieldCheck,
  arrowRight: ArrowRight,
  arrowLeft: ArrowLeft,
  file: FileText,
  clock: Clock,
  inbox: Inbox,
  info: Info,
  sparkles: Sparkles,
  plus: Plus,
  ellipsis: Ellipsis,
  pencil: Pencil,
  archive: Archive,
  trash: Trash2,
  refresh: RotateCw,
  calendar: CalendarDays,
  lock: Lock,
  layers: Layers,
  upload: Upload,
  folderPlus: FolderPlus,
  download: Download,
  eye: Eye,
  users: Users,
  play: Play,
  rejected: CircleX,
  fileUp: FileUp,
  filter: ListFilter,
  pause: Pause,
  zoomIn: ZoomIn,
  zoomOut: ZoomOut,
  fit: Maximize2,
  locate: Crosshair,
  // 批注画布工具条（prd-03 §4.1）
  pointer: MousePointer2,
  hand: Hand,
  square: Square,
  mapPin: MapPin,
  text: Type,
  undo: Undo2,
  // 时间线 / 站内信
  paperclip: Paperclip,
  megaphone: Megaphone,
  mail: Mail,
  checkAll: CheckCheck,
  send: Send,
} satisfies Record<string, LucideIconData>;

export type IconName = keyof typeof ICONS;

/** 后端 `menus[].icon` 是 lucide 的 kebab-case 名称，这里统一解析成前端图标键。 */
export function resolveIconName(raw: string | null | undefined): IconName {
  if (!raw) {
    return 'inbox';
  }
  const camel = raw.replace(/[-_]([a-z0-9])/g, (_match, char: string) => char.toUpperCase());
  return camel in ICONS ? (camel as IconName) : 'inbox';
}

@Component({
  selector: 'app-icon',
  imports: [LucideAngularModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <lucide-icon [img]="data()" [size]="size()" [strokeWidth]="strokeWidth()" />
  `,
  host: {
    class: 'inline-flex shrink-0',
    'aria-hidden': 'true',
  },
})
export class IconComponent {
  readonly name = input.required<IconName>();
  readonly size = input(18);
  readonly strokeWidth = input(1.75);

  protected readonly data = computed(() => ICONS[this.name()]);
}
