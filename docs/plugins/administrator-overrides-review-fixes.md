# 三层授权模型复审缺陷修复计划 (已实施)

> 背景: 三层模型实施 (工作树未提交) 经两轮独立审计, 共 4 项确认缺陷, 均已复现。本计划供 DeepSeek 实施, 实施前不改其他代码。
> 复现证据: F2 两方向与 F3 已用脚本实测 (允许↔拒绝方向翻转), F1 与 F4 为代码路径确认。
> 状态 (2026-10-08): F1-F4 与规则 6 全部实施完毕; 三项附带项中 1 与 2 已完成, 3 仍登记为后续项。

## F1 (P1): 保存/预览接口缺失 overrides 时静默清空例外层

`control_server.py` `_route_administrator_settings` PUT 分支 (约 1667-1675 行): `payload.get("overrides", [])` 兜底绕过了 `save_administrator_groups` 的必填保护。旧版前端缓存页或手调 API 不带 overrides 提交 → 200 + 例外区段清空 → 恢复原本被否决的授权 (fail-open)。

修法:
- PUT: `"overrides" not in payload` 时抛 ValueError (400), 与 `"groups" not in payload` 同级; 载荷白名单不变
- preview 分支同样强制 `"overrides" in payload"` (预览语义也应与保存一致)
- 注意: 缺省兜底删除后, 确认现有 e2e/前端均已显式携带 (已核实均携带)

## F2 (P1): 旧排除迁移改变启用语义

`administrator_groups.py` `migrate_legacy_scope_exclusions` (约 287-302 行) 两个已复现的方向:

- **2a**: 停用组的旧排除被迁移为 `enabled=True` 的新例外 → 原本因组停用而允许的成员被拒
- **2b**: 启用组的旧排除并入同一用户**已存在的停用**例外 → 否决永不生效, 原本被拒的成员获得授权

裁定规则 (保持"同一平台用户只有一条例外"约束不变):

1. 迁移只**创建**或**合并**, 且条目的 `enabled` 一律取**源组的 enabled**: 源组停用 → 新条目 `enabled=False` (行为不变: 否决在迁移前后都不生效, 配置被捕获待用户启用)
2. 目标用户已有例外条目时, **仅当该条目 enabled 与源组 enabled 相同**才合并 deny; 不同则**整组跳过迁移**, 组的 legacy excluded 原样保留 (读取路径非严格, resolve 仍按旧规则生效, 有效行为零变化)
3. 跳过的组要可见: `logger.warning` 记录组 ID 与冲突条目 ID; `administrator_settings_snapshot` 增加 `migration_pending: [组 ID 列表]` 字段, 界面提示"N 个组的旧排除与现有例外的启用状态冲突, 未自动迁移; 请调整对应例外条目的启用状态后保存" (用户启用/停用对应例外后, 下次读取即可迁移)
4. 生成条目的 `legacy-<digest>` ID 在创建前检查与现有条目 ID 冲突, 冲突时在 digest 中并入组 ID 重算
5. 幂等性保持: 同一配置重复迁移不产生新条目 (现有测试已锁)
6. **strict_scope 放宽为"只拒绝新增"** (DeepSeek 预审发现, 裁定确认): 规则 2 的整组跳过会让 legacy excluded 原样留在快照里, 前端草稿必然带着它回存, 而 `prepare_administrator_groups` 现行 strict 无条件拒绝一切 selected+excluded → 只要存在待迁移组, 任何保存都 400, 规则 3 给出的恢复指引 ("调整例外启用状态后保存") 本身就是被拒的操作, 形成死锁; 前端唯一出口 (逐插件点"不表态") 会永久丢弃未迁移的否决, 与"行为零变化"冲突。修法: `normalize_administrator_groups` 的 strict 检查改为与 `previous` (已保存配置归一化结果, prepare 已持有) 按组 ID 比对——提交的 excluded 是该组已保存 excluded 的**子集**则放行 (原样往返与逐项收敛都合法), 否则抛出并点名新增项; 新组/改名组视为空 previous, 任何 excluded 都拒绝。预览端点同一判定, 对称生效。附带收益: 通用 PUT /config 对存量配置的 round-trip 也随之解除 (见附带项 2)。报错文案区分新旧: "指定插件模式不能新增排除插件: <名单>; 旧有排除可原样保留, 新否决请使用成员例外"。

语义说明 (写进 system-administrators.md 迁移节): 停用组的排除迁移为停用例外后, 再启用该组**不会**恢复旧排除, 需单独启用对应例外条目。

## F3 (P2): 账号保护跨条目泄漏

`administrator_groups.py` `protected_users` (约 516-521 行): 候选从 `protect=true` 条目收集, 但匹配用的是"任一启用条目", 导致"停用保护组 + 启用不保护组"的成员仍受保护 (已复现)。

修法: 保护判定收敛到**同一条目内**同时满足 启用 + 实例绑定匹配 + protect, 不再复用 `_matching`/`_matching_overrides` 的任一语义:

```python
instance = self._current_instance(platform_id)
if instance is None:
    return []
protected = {member["user_id"] for group in groups if group["enabled"] and group["protect"]
             for member in group["members"]
             if member["platform_id"] == platform_id and member["platform_instance_id"] == instance}
protected.update(item["user_id"] for item in overrides
                 if item["enabled"] and item["protect"] and item["platform_id"] == platform_id
                 and item["platform_instance_id"] == instance)
return sorted(protected)
```

## F4 (P2): 权限预览丢失否决来源

`administrator_settings.py` `preview_administrator_groups` (约 262-269 行): 只列 `grant.allowed` 的插件, 被否决的插件静默消失, `sources.deny` 恒为空 (死代码), 设计稿 §6 的"允许来源/否决来源/最终无效"展示未实现。

修法:
- 收录条件从 `grant.allowed` 放宽为"任一来源非空" (`group_ids or allowed_overrides or excluded_by or denied_overrides`), 且 `plugin.permissions.supports_administrators`
- 每个插件条目增加 `allowed: bool` 字段; 被否决的插件照常携带 `sources` (此时 deny 非空)
- 前端预览渲染: `allowed=false` 的插件用错误/弱化样式标记"无效", 否决来源正常显示; `satrap-ui/src/api/administrators.ts` 的 `AdministratorPreview` 类型补 `allowed`
- e2e mock 与断言同步 (补一条"组允许 + 例外否决 → 预览显示无效与两侧来源"的用例)

## 附带项 (随本轮一并处理)

1. `tests/benchmark/results/plugin_hotpath/after-overrides.json` 入库 (沿用 after-batch 惯例, 作为本轮哈希一致证据)
2. `docs/plugins/system-administrators.md` 迁移节补注: 存量配置的旧排除在迁移完成前可经任意保存路径原样往返 (F2 规则 6), 但手工新增"指定插件模式的排除"会被严格校验拦下 (400), 新否决请改用成员例外
3. pyright 警告较基线 +210 (5874→6084, 均为 reportUnknown*, 集中在 Any 密集区, 非门禁; error 297 零新增): 登记为后续清理项, 本轮不处理

## 测试清单

- F1: PUT 缺 overrides → 400; preview 缺 overrides → 400; 携带空列表显式清空 → 200 (合法路径)
- F2: 停用组旧排除 → 新条目 enabled=False 且 resolve 仍允许; 启用组 + 已存在停用条目 → 迁移跳过, 组 excluded 保留, resolve 仍拒绝, 快照含 migration_pending; 同 enabled 合并 → deny 并集生效; 跳过后再调整条目启用状态 → 下次读取完成迁移; ID 冲突重算
- F2 规则 6 (死锁解除): 待迁移组 excluded 原样往返 → 保存 200 且语义不变; 逐项移除旧排除 (子集) → 200; 给待迁移组新增排除项 → 400 且报错点名新增项; 新建组/改名组携带 excluded → 400; preview 同口径; 通用 PUT /config 携带存量旧排除 round-trip → 200
  以上六条除单元级覆盖外, 端点级合并由 `test_rule6_section_endpoint_round_trips_converges_and_rejects_added_exclusion` 锁定 (PUT 往返 200 / 新增 400 点名 / 改名组 400 / 子集收敛 200 / preview 三个同口径断言), 通用 PUT /config 往返与手工新增拒绝分别由 `test_pending_legacy_exclusions_round_trip_through_general_config` 与 `test_general_config_rejects_hand_added_selected_exclusion` 锁定。
- F3: 停用 protect 组 + 启用不保护组 → 不保护; 停用 protect 例外 + 启用组成员 → 不保护; 启用 protect 条目 + 实例匹配 → 保护; 实例代次不符 → 不保护
- F4: 组允许 + 例外否决的插件出现在预览, allowed=false, sources 双侧非空

## 门禁

pytest 全量 (排除 2 条环境红) / pyright `-p .pyrightcfg` 297 error 零新增 / tsc 0 / vitest / `administrators.mjs` e2e / benchmark 对 `after-batch7.json` 哈希一致 (F2/F3 不触基准路径的输入构造, 预期一致)。
