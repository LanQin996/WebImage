import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DEFAULT_UPSTREAM_MODEL_NAME,
  resolveDefaultUpstreamModelName,
} from "./upstream-model-default.ts";

test("默认上游模型名兜底为 auto，与后端一致", () => {
  assert.equal(DEFAULT_UPSTREAM_MODEL_NAME, "auto");
});

test("读取配置时空值不落成文本模型名", () => {
  // undefined / null / 空串 / 纯空白都必须回到 auto，否则保存后会把文本模型名发给上游生图接口
  for (const value of [undefined, null, "", "   "]) {
    assert.equal(resolveDefaultUpstreamModelName(value), "auto");
  }
});

test("归一化会去掉首尾空白并保留用户显式填写的模型名", () => {
  assert.equal(resolveDefaultUpstreamModelName("  auto  "), "auto");
  assert.equal(resolveDefaultUpstreamModelName("gpt-image-2.5-flare"), "gpt-image-2.5-flare");
  // 只兜底空值：用户显式指定 gpt-6 时原样透传，不替用户改写
  assert.equal(resolveDefaultUpstreamModelName("gpt-6"), "gpt-6");
});
