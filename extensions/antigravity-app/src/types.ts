export type TabKey = 'roots' | 'library' | 'tasks' | 'reviews';

export interface OverviewData {
  tab?: TabKey;
  data_dir: string;
  schema_version: number;
  default_suffixes: string[];
  counts: {
    roots: number;
    enabled_roots: number;
    sources: number;
    archived_sources: number;
    unfinished_tasks: number;
    reviews: number;
  };
}

export interface SourceRoot {
  root_id: string;
  name: string;
  path: string;
  recursive: boolean;
  suffixes: string[];
  enabled: boolean;
  created_at: string;
  updated_at: string;
  last_synced_at: string | null;
}

export interface ScanItem {
  root_id: string;
  root_name: string;
  path: string;
  relative_path: string;
  name?: string;
  suffix?: string;
  size?: number;
  status: 'new' | 'changed' | 'unchanged' | 'missing' | 'error';
  source_id?: string;
  version_id?: string;
  sequence?: number;
  archived?: boolean;
  error?: string;
}

export interface ScanReport {
  roots: Array<{
    root: SourceRoot;
    exists: boolean;
    counts: { new: number; changed: number; unchanged: number; missing: number; errors: number };
    error?: string;
  }>;
  counts: { new: number; changed: number; unchanged: number; missing: number; errors: number };
  items: ScanItem[];
}

export interface SyncReport {
  imported: number;
  updated: number;
  indexed: number;
  skipped: number;
  errors: number;
  missing_on_disk: number;
  results: Array<{
    root_id: string;
    path: string;
    relative_path: string;
    action: string;
    source_id?: string;
    version_id?: string;
    sequence?: number;
    error?: { code: string; message: string };
  }>;
}

export interface BrowseResult {
  current: string | null;
  parent: string | null;
  entries: Array<{ name: string; path: string; kind: 'shortcut' | 'drive' | 'dir' }>;
}

export interface CatalogItem {
  source_id: string;
  title: string;
  archived: number | boolean;
  version_id: string;
  original_name: string;
  size: number;
}

export interface SourceVersion {
  source_id: string;
  version_id: string;
  sequence: number;
  sha256: string;
  size: number;
  original_name: string;
  created_at: string;
}

export interface SourceStatusData {
  version: SourceVersion;
  artifacts: Array<{
    artifact_id: string;
    backend: string;
    scope: string;
    args: Record<string, unknown>;
    state: string;
    indexed_entries: number;
  }>;
  failures: Array<{
    backend: string;
    scope: string;
    args: Record<string, unknown>;
    error: { code?: string; message?: string };
  }>;
}

export type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'cancelled';

export interface TaskDTO {
  task_id: string;
  title: string;
  content: string;
  status: TaskStatus;
  due: string | null;
  due_kind: 'date' | 'datetime' | null;
  tags: string[];
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface TaskLogDTO {
  log_id: string;
  task_id: string;
  task_revision: number;
  content: string;
  corrects: string | null;
  created_at: string;
}

export interface ReviewSummary {
  review_id: string;
  created_at: string;
}

export interface ReviewDetail {
  review_id: string;
  created_at: string;
  sections: Record<string, Array<{ kind: 'user_statement' | 'source_excerpt' | 'ai_inference'; text: string }>>;
  snapshots: Array<{ task: TaskDTO; logs: TaskLogDTO[] }>;
}
