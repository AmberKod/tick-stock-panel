// 热点工作区展示标签 / 配色映射。
// 取值口径与后端 app/services/hotspot (models.QUALITY_* / scoring.classify_stage /
// scoring.assign_role) 保持一致; 未知取值一律回落到中性样式, 不臆造语义。

/** quality_status → 中文标签 + tailwind 色 */
export const QUALITY_LABEL: Record<string, { text: string; className: string }> = {
  available: { text: '数据完整', className: 'bg-bull/15 text-bull' },
  partial: { text: '部分字段缺失', className: 'bg-warning/15 text-warning' },
  stale: { text: '缓存过期', className: 'bg-warning/15 text-warning' },
  failed: { text: '同步失败', className: 'bg-danger/15 text-danger' },
  missing_mapping: { text: '缺少数据源映射', className: 'bg-elevated text-muted' },
}

/** 生命周期阶段 → 配色(阶段越靠后越冷) */
export const STAGE_CLASS: Record<string, string> = {
  初次异动: 'bg-elevated text-secondary',
  确认扩散: 'bg-accent/15 text-accent',
  加速主升: 'bg-bull/15 text-bull',
  分歧放量: 'bg-warning/15 text-warning',
  降温退潮: 'bg-danger/15 text-danger',
}

/** 后端 state 字段 → 中文标签; 空串表示源未提供 */
export const STATE_LABEL: Record<string, string> = {
  persistent_hot: '持续热门',
  weakening: '走弱',
  cooling: '降温',
  emerging: '新起',
}

/** 成分股角色 → 配色 */
export const ROLE_CLASS: Record<string, string> = {
  核心龙头: 'bg-bull/15 text-bull',
  助攻: 'bg-accent/15 text-accent',
  补涨: 'bg-elevated text-secondary',
  后排: 'bg-elevated text-muted',
  掉队: 'bg-danger/10 text-danger',
}

export function qualityOf(status: string | null | undefined) {
  return QUALITY_LABEL[status ?? ''] ?? { text: status || '未知', className: 'bg-elevated text-muted' }
}

export function stageClass(stage: string | null | undefined) {
  return STAGE_CLASS[stage ?? ''] ?? 'bg-elevated text-secondary'
}

export function stateLabel(state: string | null | undefined) {
  if (!state) return null
  return STATE_LABEL[state] ?? state
}

export function roleClass(role: string | null | undefined) {
  return ROLE_CLASS[role ?? ''] ?? 'bg-elevated text-muted'
}
