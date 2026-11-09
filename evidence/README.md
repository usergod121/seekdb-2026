# 证据日志（journal）

规则要求 `conversation.json` 里的每条证据都能用 JSON Pointer + ≤240 字符原文定位，而且内容必须真实。
所以从这一版开始，每个开发环节都在这里留一条**原始记录**，最后再按五维结构整理成 `agent/conversation.json`。

条目格式（JSONL，一行一条）：

```json
{"t":"2026-11-09T07:10:00Z","actor":"user|agent|tool","kind":"prompt|hypothesis|command|result|correction|retest|reflection",
 "content":"<原始文本，≤240 字符以内会被直接引用>","file":"<可选的输出文件路径>","episode":"EP1"}
```

约定：
- `actor=user` 的记录只写**用户本人真实的原话**，不做润色；
- `actor=tool` 的记录是命令+真实输出摘要（数字不得改写）；
- `kind=correction` / `retest` 用来标记"解释结果→修正→复测"闭环，这类链条能拿到 1.5~2 分档；
- 只有当上面三类都存在时，才把该 `episode` 写进 `conversation.json`。

当前已归档的 episode（见 `agent/conversation.json`）：EP1~EP7（问题拆解 / 独立验证 / 实验迭代 / 迁移 / 取舍反思）。
后续新增实验按同样格式追加。
