/**
 * 「默认请求上游模型名称」的兜底与归一化。
 *
 * 空值兜底必须是上游网页生图能识别的 `auto`，不能落成文本模型名：`gpt-image-2` 生图时
 * 这个值会原样作为 `model` 发给 `/backend-api/f/conversation`，文本模型名会被上游用
 * 伪装 404 `{"detail": "Model not found"}` 拒绝。
 *
 * 这个文件刻意不依赖 `@/` 别名和浏览器 API，便于直接用 node:test 做单元测试。
 */

/** 兜底上游模型名，与后端 `default_upstream_model_name` 的默认值保持一致。 */
export const DEFAULT_UPSTREAM_MODEL_NAME = "auto";

/** 归一化「默认请求上游模型名称」：空值回落到 `auto`，其余原样透传给上游。 */
export function resolveDefaultUpstreamModelName(value: unknown): string {
  return String(value ?? "").trim() || DEFAULT_UPSTREAM_MODEL_NAME;
}
