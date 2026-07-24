export const ACTIVE_STOCK_PICKER_TASK_KEY =
  'longbridge.stock-picker.active-analysis';

export interface ActiveStockPickerTask {
  job_id: string;
  runtime_id: string;
  created_at: string;
}

function isActiveStockPickerTask(value: unknown): value is ActiveStockPickerTask {
  if (!value || typeof value !== 'object') return false;
  const task = value as Record<string, unknown>;
  return (
    typeof task.job_id === 'string'
    && task.job_id.length > 0
    && typeof task.runtime_id === 'string'
    && task.runtime_id.length > 0
    && typeof task.created_at === 'string'
    && !Number.isNaN(Date.parse(task.created_at))
  );
}

export function loadActiveStockPickerTask(
  storage: Storage = window.sessionStorage,
): ActiveStockPickerTask | null {
  try {
    const raw = storage.getItem(ACTIVE_STOCK_PICKER_TASK_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (isActiveStockPickerTask(parsed)) return parsed;
    storage.removeItem(ACTIVE_STOCK_PICKER_TASK_KEY);
  } catch {
    try {
      storage.removeItem(ACTIVE_STOCK_PICKER_TASK_KEY);
    } catch {
      // Storage can be unavailable in privacy-restricted browser contexts.
    }
  }
  return null;
}

export function saveActiveStockPickerTask(
  task: ActiveStockPickerTask,
  storage: Storage = window.sessionStorage,
): void {
  try {
    storage.setItem(ACTIVE_STOCK_PICKER_TASK_KEY, JSON.stringify(task));
  } catch {
    // The live page can still track the task when storage is unavailable.
  }
}

export function clearActiveStockPickerTask(
  storage: Storage = window.sessionStorage,
): void {
  try {
    storage.removeItem(ACTIVE_STOCK_PICKER_TASK_KEY);
  } catch {
    // No recovery record exists when storage is unavailable.
  }
}
