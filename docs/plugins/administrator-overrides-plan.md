# 系统管理员授权三层模型设计稿 (裁定点已定稿, 待实施)

> 状态: 设计已定稿 (2026-10-08 四点裁定见第 8 节), 未实施。后端实施交由 DeepSeek, 前端后端落地后补。
> 动机: 现行"管理组 plugin_scope 同时携带 included/excluded"模型中, 指定插件模式下的排除既冗余 (单组时与不表态等效) 又反直觉 (排除作用对象是人不是组)。本设计把职责拆清: **组只负责授予, 人只负责例外**。账号保护从隐式行为升级为组和例外的显式开关。

## 1. 目标模型

授权判定保持三层, 自底向上:

| 层 | 职责 | 现状 |
| --- | --- | --- |
| 插件本地名单 | 插件各自配置文件中的调用者名单 | 不动 |
| 管理组 | **只授予**: 指定插件=白名单; 所有插件=全集减排除项 | 简化, 删掉指定模式的排除 |
| 成员例外 | **对人的允许与否决**, 优先裁决层 | 新增 |

任一层的"否决/排除"都压过所有"允许" (沿用现行排除优先原则, 见 `administrator_groups.py` resolve 的 `bool(allowed) and not excluded`)。

系统管理员授权仍只免去插件调用者名单; 功能开关, 平台能力, 成员所有权与审批不放宽。插件本地名单独立生效, 不受本模型影响。

## 2. 配置结构

### 2.1 管理组 (简化)

`administrator_groups` 区段结构不变, 新增一条校验不变量:

```yaml
administrator_groups:
  - id: interns
    name: 实习管理员
    enabled: true
    protect: true             # 新增: 组成员是否进入好友删除保护名单, 缺省 true
    members: [...]
    plugin_scope:
      mode: selected          # selected: 只允许; all: 只排除
      included: [friend_manager]
      excluded: []            # mode=selected 时必须为空 (新增校验)
```

- `mode=all and included` 非空 → 已有 ValueError, 保留
- `mode=selected and excluded` 非空 → **新增** ValueError ("指定插件模式不能携带排除列表, 请使用成员例外"); 仅对保存/预览的新提交强制, 存量配置走迁移 (第 5 节), 不允许启动失败
- `protect` 缺省按 true 处理 (与现行"组成员全部受保护"行为一致); 停用组的保护不生效 (enabled 优先)

### 2.2 成员例外 (新增顶层区段)

```yaml
administrator_overrides:
  - id: intern-zhangsan              # 规则同管理组 ID
    enabled: true                    # 可临时停用而不删除条目, 缺省 true
    protect: false                   # 是否进入好友删除保护名单, 缺省 false
    platform_id: onebot-main
    platform_instance_id: 由服务保存时绑定
    user_id: "10001"
    allow: [group_chat]              # 额外允许, 可空
    deny: [group_admin]              # 否决, 可空
```

校验规则 (`normalize_administrator_overrides`, 新函数, 与组归一化并列):

- 顶层必须是列表, 上限 1000 条; 未知字段拒绝
- `id` 格式与去重规则同管理组 ID (`_GROUP_ID`)
- `(platform_id, user_id)` 全列表内不得重复 (一个人只有一条例外)
- `allow` / `deny` 各自为无重复插件名列表 (复用 `_names` 语义)
- **`allow ∩ deny` 重叠不做硬校验** (裁定点 A): 前端三态控件从构造上杜绝重叠, 后端归一化时检测到重叠只 `logger.error` 记录误配, 求值按否决优先处理 (与全局排除优先一致), 不拒绝配置不中断启动
- `allow` 与 `deny` 同时为空 → 该条目无授权意义; `protect: true` 时仍保留 (纯保护条目合法), 否则归一化时丢弃
- `enabled` / `protect` 必须是布尔值, 缺省分别为 true / false
- 身份绑定与组成员完全同构: `platform_instance_id` 保存时服务端绑定, `bind_new` / `previous` / `normalize_user` 参数语义与 `normalize_administrator_groups` 一致, 平台重建后身份失效, 需显式重绑

## 3. 求值语义 (resolve 扩展)

`AdministratorService` 持有 (groups, overrides) 双快照, `apply(groups, overrides)` 原子替换; `administrator_revision(groups, overrides)` 覆盖两区段。

```
matched_groups    = 启用且身份匹配 (平台实例 + 用户 ID) 的组       # 现行 _matching 逻辑不变
matched_overrides = 启用且身份匹配 (同一套实例绑定校验) 的例外条目
allowed = 任一 matched_groups 允许 (all 模式或 included 命中)
       OR 任一 matched_overrides 的 allow 命中
denied  = 任一 matched_groups 的 excluded 命中 (迁移后仅 all 模式可携带)
       OR 任一 matched_overrides 的 deny 命中
grant   = allowed AND NOT denied
```

`AdministratorGrant` 扩展来源归属 (预览与排障用):

```python
@dataclass(frozen=True)
class AdministratorGrant:
    allowed: bool
    group_ids: tuple[str, ...] = ()            # 允许来源组 (现行)
    excluded_by: tuple[str, ...] = ()          # 否决来源组 (现行)
    allowed_overrides: tuple[str, ...] = ()    # 允许来源例外条目 ID (新增)
    denied_overrides: tuple[str, ...] = ()     # 否决来源例外条目 ID (新增)
    revision: str = ""
```

消费方 `plugin_authorization.py` 只读 `grant.allowed`, 无需改动。

**个人 allow 可独立授予**: 不属于任何组的人, 仅凭例外条目的 allow 即可获得对应插件的系统管理员授权 (裁定点 D: 例外层同时支持 allow 与 deny; 只做 deny 则无法表达"给某个不在组里的人临时授权", 且两者同构, 成本几乎相同)。

**账号保护改为显式开关** (裁定点 B): `protected_users()` 从"组成员全部受保护"改为: **启用且 `protect` 为真的组的成员, 并上启用且 `protect` 为真的例外条目身份**, 两处身份都仍需通过当前实例绑定校验。组缺省 `protect: true` 保持现行行为; 例外条目缺省 `protect: false` (例外默认只是授权微调, 要保护需显式开启)。`friend_manager` 等消费方不变, 仍取并集。

## 4. 接口变更

沿用现有端点与修订机制, 只扩展载荷 (单一文档, 单一 expected_revision, 409/400 语义不变):

| 接口 | 变更 |
| --- | --- |
| `GET /config/administrator-groups` | 快照增加 `overrides`, `invalid_overrides` (失效身份, 同 `invalid_members` 结构+条目 id), `migrated_from_legacy: true` 标记 (发生过迁移时, 供界面提示) |
| `PUT /config/administrator-groups` | 载荷增加 `overrides` (完整列表) 与 `rebind_overrides` (显式重绑, 机制同 `rebind_members`) |
| `POST .../preview` | 载荷同 PUT; 返回的每个身份增加 `override_id` (命中时) 和每插件的 `sources: { allow: ["group:xxx", "override:yyy"], deny: [...] }` |
| `POST .../apply` | 不变, 应用后的运行快照含例外层 |

`administrator_settings.py` 对应改动: `administrator_settings_snapshot` / `prepare_administrator_groups` (改名或并列 `prepare_administrator_overrides`) / `save_administrator_groups` / `preview_administrator_groups` 全部携带双区段; 预览的身份集合 = 组成员 ∪ 例外条目人员 (例外-only 的人也要出现在预览里)。

## 5. 存量迁移

旧配置中 `mode=selected` 且 `excluded` 非空的组, **读取路径自动迁移** (启动加载与设置页读取共用同一函数, 保证运行时与界面行为一致):

```
对每组 (selected 且 excluded 非空):
  对组内每个成员 × excluded 中每个插件:
    生成/合并例外条目: (platform_id, user_id) 已有条目则并入其 deny, 否则新建
  组 excluded 清空
```

- 迁移只在内存生效, 下次保存时落盘; 界面凭 `migrated_from_legacy` 提示"已将 N 条组排除迁移为成员例外, 保存后生效"
- 同组 overlap (included ∩ excluded) 迁移后: allow 留组, deny 到人, deny 优先, 有效行为不变
- 旧配置组没有 `protect` 字段, 归一化缺省 true, 保护行为与升级前完全一致, 无需迁移动作
- **语义差异须文档化** (裁定点 C, 已接受): 迁移按当前成员展开, 此后新加入该组的成员不再自动被否决。组级"跟成员走"的否决能力随本设计移除

## 6. 前端变更 (后端完成后实施)

- 组的插件卡片三态控件按模式收敛: `selected` → [不表态 | 允许]; `all` → [包含 | 排除]。控件抽成共享组件 (现位于 `AdministratorsPanel.tsx` 内部)
- 组卡片头部增加"账号保护"开关 (`protect`, 复用 `components/ui/Toggle.tsx`), 默认开; 例外条目卡片同款开关, 默认关
- 新增"成员例外"区块: 例外条目卡片 (启用开关 + 账号保护开关 + 平台选择 + 用户识别号 + 失效/重绑提示, 与组成员同款交互), 每卡片内对支持系统管理员的插件渲染同一三态控件 ([不表态 | 允许 | 排除])
- 「预览有效权限」按人展示来源归属, 例如 "group_admin: 允许 (组: 全体管理员), 否决 (例外) → 无效", 解决"算不清"问题
- e2e `administrators.mjs` 更新 + 例外层, 保护开关与迁移提示的新用例

## 7. 测试清单 (后端, 交接给 DeepSeek)

- `normalize_administrator_overrides`: 重复身份 / 未知字段 / 空条目处理 (`protect: true` 保留, 否则丢弃) / enabled 与 protect 缺省与类型校验 / 实例绑定与重绑 (对齐现有组测试风格); **allow∩deny 重叠: 不报错, 记 error 日志, resolve 按否决优先** (裁定点 A)
- `resolve`: 个人 allow 独立授予; 个人 deny 否决组允许; 组 all 模式排除与个人 deny 并存; 停用 (`enabled: false`) 的组与例外均不命中; 实例代次不匹配则不命中; 来源归属字段正确
- `protected_users`: 组 protect=false 后成员不再受保护; 例外 protect=true 的身份进入保护; 停用条目不保护; 实例失效不保护
- 迁移: selected+excluded 展开为逐人 deny 且组清空; 同组 overlap 行为不变; 已存在的 (platform_id, user_id) 条目正确合并; 旧组无 protect 字段时按 true 处理
- preview: 例外-only 身份出现; sources 归属正确
- 修订: groups 或 overrides 任一变化, section_revision 变化

## 8. 裁定点 (2026-10-08 已全部定稿)

- **A. 个人层 allow∩deny 重叠**: 不做硬校验。前端三态控件从构造上杜绝重叠; 后端归一化检测到重叠时 `logger.error` 记录误配, 求值按否决优先, 不拒绝配置不中断启动
- **B. 账号保护**: 从隐式行为升级为显式开关。组新增 `protect` (缺省 true, 保持现行行为); 例外条目新增 `protect` (缺省 false); `protected_users()` 取两者并集
- **C. 迁移按当前成员展开, 新成员不继承否决**: 已接受, 迁移提示中明示
- **D. 例外层同时支持 allow 与 deny**: 已接受

## 9. 实施落点 (交接锚点)

| 文件 | 改动 |
| --- | --- |
| `satrap/core/config/administrator_groups.py` | 新增 `normalize_administrator_overrides`; `AdministratorService` 双快照; `resolve` 扩展; `AdministratorGrant` 来源字段; `administrator_revision` 覆盖双区段 |
| `satrap/core/config/administrator_settings.py` | 快照/prepare/save/preview 携带 overrides; 迁移函数; `invalid_overrides` |
| `satrap/core/backend/control_server.py` | 端点载荷透传 (约 1672 行起的路由) |
| `satrap/core/plugin_authorization.py` | 预期无改动, 核验 `grant.allowed` 唯一消费点 |
| `docs/plugins/system-administrators.md` | 语义段与配置结构段改写 |
| `docs/archive/development/plugin-management-permission-contracts.md` | 第 6 节管理组规则补新一轮契约 |
| `tests/unit/test_administrator_groups.py` | 第 7 节清单 |
| `satrap-ui` | 第 6 节, 后端落地后另行实施 |
