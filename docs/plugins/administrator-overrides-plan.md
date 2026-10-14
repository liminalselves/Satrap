# 系统管理员授权三层模型设计稿 (已实施, 待验收归档)

> 状态: 修订 2 (2026-10-08), **后端与前端均已实施完毕**, 规格已并入
> [系统管理员与插件管理权限](system-administrators.md); 本稿保留作实施记录, 验收后移入归档。
> 修订 2 相对修订 1 的增补: 机制裁定 (严格校验与读路径迁移分离, 裁定 E), PermissionGrant 来源扩展 (裁定 F),
> 第 9 节锚点表补齐 3 个漏点文件 (BackendManager / document.validate_config_document / loader 默认模板),
> 第 7 节按实际核验列出会破的既有测试调用点 (4 组硬破 + 1 处断言静默失效, 其余用关键字缺省不需改),
> 前端与文档落点裁定, 以及新增的第 10 节调用点读写标注表。
> 实施期增补 (见第 12 节): 新增参数一律 keyword-only, 授权来源贯通到统一权限服务, 前端三态控件抽为共享组件。
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
- `mode=selected and excluded` 非空 → **新增** ValueError ("指定插件模式不能携带排除列表, 请使用成员例外"); **仅在 `strict_scope=True` 时抛出**, 见裁定 E
- 存量配置走迁移 (第 5 节), 读路径一律容忍该组合, 不允许启动失败
- `protect` 缺省按 true 处理 (与现行"组成员全部受保护"行为一致); 停用组的保护不生效 (enabled 优先)

`normalize_administrator_groups` 新增关键字参数 `strict_scope: bool = False`。该参数只控制"selected 携带 excluded"这一条硬校验, 其余校验 (字段白名单, mode 取值, 列表去重, all 禁 included, ID 格式, 成员类型, 实例绑定) 在任何模式下都执行。

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

函数签名:

```python
def normalize_administrator_overrides(
    raw: object, platforms: Sequence[Mapping[str, Any]], *, bind_new: bool = False,
    previous: Sequence[Mapping[str, Any]] = (),
    normalize_user: Callable[[Mapping[str, Any], str], str] | None = None,
    strict_scope: bool = False,
) -> list[dict[str, Any]]:
```

`strict_scope` 在例外侧当前不启用任何拒绝规则 (唯一候选 allow∩deny 按裁定 A 明确不硬校验), 参数与组归一化保持同形, 便于 `prepare` 系列统一传参; 若评审认为属空转参数, 可只保留在组归一化上 (第 8 节裁定 E 备注)。

## 3. 求值语义 (resolve 扩展)

`AdministratorService` 持有 (groups, overrides) 双快照。接口变化:

```python
def __init__(self, platforms, groups=None, overrides=None) -> None: ...
def apply(self, groups: object, overrides: object = ()) -> str: ...        # 原子替换双区段
def snapshot(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]: ...   # (groups, overrides, revision)
def resolve(self, origin, plugin_name) -> AdministratorGrant: ...
def protected_users(self, platform_id: str) -> list[str]: ...
```

`snapshot()` 由 2 元组变 3 元组, 现有 5 处调用点需同步 (第 10 节)。

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

该 dataclass 现有两处位置参数构造 (`administrator_groups.py:279`/`284`), 均在本次改动的函数内, 一并更新为关键字构造。

**来源归属贯通到统一授权 (裁定 F)**: `plugin_authorization.py` 的 `PermissionGrant` 增加 `override_ids: tuple[str, ...] = ()`, 其 `source` 在仅由例外层授权时取新标签 `administrator_override`:

```python
@dataclass(frozen=True)
class PermissionGrant:
    permission: str
    allowed: bool
    source: str = ""
    group_ids: tuple[str, ...] = ()
    override_ids: tuple[str, ...] = ()         # 新增
```

- 赋值 (`plugin_authorization.py:132-134`): 组允许时 `source="administrator_group"`, `group_ids=admin.group_ids`, `override_ids=admin.allowed_overrides`; 组未允许而例外允许时 `source="administrator_override"`, `group_ids=()`, `override_ids=admin.allowed_overrides`
- 授权指纹 (`plugin_authorization.py:139-140`) 与 debug 日志 (`plugin_authorization.py:411-414`) 同步纳入 `override_ids`, 否则"由例外授予"与"由组授予"会得到相同指纹, 持久申请复核无法区分授权依据
- 消费方只读 `grant.allowed` 的假设不成立: 除 `allowed` 外, 现有代码还读 `group_ids` (138/140/414 行) 与 `revision` (142 行), 本次一并覆盖

**个人 allow 可独立授予**: 不属于任何组的人, 仅凭例外条目的 allow 即可获得对应插件的系统管理员授权 (裁定点 D: 例外层同时支持 allow 与 deny; 只做 deny 则无法表达"给某个不在组里的人临时授权", 且两者同构, 成本几乎相同)。

**账号保护改为显式开关** (裁定点 B): `protected_users()` 从"组成员全部受保护"改为: **启用且 `protect` 为真的组的成员, 并上启用且 `protect` 为真的例外条目身份**, 两处身份都仍需通过当前实例绑定校验。组缺省 `protect: true` 保持现行行为; 例外条目缺省 `protect: false` (例外默认只是授权微调, 要保护需显式开启)。`friend_manager` 等消费方不变, 仍取并集。

## 4. 接口变更

沿用现有端点与修订机制, 只扩展载荷 (单一文档, 单一 expected_revision, 409/400 语义不变):

| 接口 | 变更 |
| --- | --- |
| `GET /config/administrator-groups` | 快照增加 `overrides`, `invalid_overrides` (失效身份, 结构 `{override_id, platform_id, platform_instance_id, user_id}`, 与 `invalid_members` 同构), `migrated_from_legacy: true` 标记 (发生过迁移时, 供界面提示) |
| `PUT /config/administrator-groups` | 载荷增加 `overrides` (完整列表) 与 `rebind_overrides` (显式重绑, 机制同 `rebind_members`, 条目 `{override_id, platform_id, user_id}`) |
| `POST .../preview` | 载荷同 PUT; 返回的每个身份增加 `override_id` (命中时) 和每插件的 `sources: { allow: ["group:xxx", "override:yyy"], deny: [...] }` |
| `POST .../apply` | 不变, 应用后的运行快照含例外层 |
| `PUT /config` (通用) | 两个区段都过 `prepare` (严格模式) 并写回 `merged_config` |

`administrator_settings.py` 对应改动 (顶层入口双区段并行, 现有函数不改名):

- `administrator_settings_snapshot(config, catalog=None)`: 返回体增加 `overrides` / `invalid_overrides` / `migrated_from_legacy`; 读路径调迁移函数
- `prepare_administrator_groups(config, groups, rebind_members=None)`: `strict_scope=True`, 只处理组区段
- `prepare_administrator_overrides(config, overrides, rebind_overrides=None)` (**并列新增**): 同一套重绑机制, `strict_scope=True`, 只处理例外区段
- `save_administrator_groups(path, groups, *, overrides, expected_revision, rebind_members=None, rebind_overrides=None)`: 两区段各自 prepare 后一次保存 (单文档单修订); 新增参数 keyword-only (见下"签名裁定")
- `preview_administrator_groups(config, groups, *, overrides, rebind_members=None, rebind_overrides=None)`: 身份集合 = 组成员 ∪ 例外条目人员 (例外-only 的人也要出现在预览里); 预览服务用 `AdministratorService(...)` 注入两区段; 新增参数 keyword-only
- `migrate_legacy_scope_exclusions(groups, overrides)` (第 5 节)
- `invalid_overrides` 与 `invalid_members` 同构生成 (实例代次比对 `available.get(platform_id) != platform_instance_id`)

签名裁定 (必填与缺省的取舍):

- `save_administrator_groups` 与 `preview_administrator_groups` 的 `overrides` 为**必填且 keyword-only**: 若给缺省 `()`, 任何忘记传参的调用方会在保存时**静默清空整个例外区段**, 与前端 YAML 回写漏字段属同一类数据丢失风险, 因此用必填把它变成显式契约。**进一步用 keyword-only 消除误绑窗口**: 旧式位置调用 `save(path, draft(), rev)` 在"位置参数 + 缺省"写法下会把修订串绑到 `overrides` 上静默放行; keyword-only 后同一调用立即抛 `TypeError`, 报错直白且不可能误绑。签名形态:

```python
def save_administrator_groups(
    path: str | Path, groups: object, *, overrides: object, expected_revision: str,
    rebind_members: object = None, rebind_overrides: object = None,
) -> dict[str, Any]: ...

def preview_administrator_groups(
    config: Mapping[str, Any], groups: object, *, overrides: object,
    rebind_members: object = None, rebind_overrides: object = None,
) -> dict[str, Any]: ...
```

  代价是既有测试调用点需补关键字实参 (第 7 节)
- `AdministratorService.apply(groups, overrides=())` 与 `__init__(..., overrides=None)` 保留缺省: 生产调用方只有 `AdministratorService.__init__` 与 `BackendManager` 486 行两处, 均在本改动内并已列入第 10 节标注表, 不存在遗漏路径; 测试中 `apply([])` 表达的正是"无管理员"语义, 缺省 `()` 结果正确
- `administrator_revision(groups, overrides)` 的 `overrides` 为**必填**: 它参与 `apply_saved_administrator_groups` 的修订比对, 若可缺省则忘记传参会算出忽略例外层的半个修订, 导致热应用误判为"未变化"; 必填后 `administrator_groups.py:213/229` 与测试 `:94` 一并更新

## 5. 存量迁移

旧配置中 `mode=selected` 且 `excluded` 非空的组, **读取路径自动迁移** (启动加载与设置页读取共用同一函数, 保证运行时与界面行为一致):

```python
def migrate_legacy_scope_exclusions(
    groups: Sequence[Mapping[str, Any]], overrides: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
```

```
# Step.1 逐组展开旧排除: 对每组 (selected 且 excluded 非空)
对组内每个成员 × excluded 中每个插件:
  生成/合并例外条目: (platform_id, user_id) 已有条目则并入其 deny, 否则新建
组 excluded 清空
# Step.2 返回迁移后的两个区段
```

- 迁移是独立函数, 不塞进纯校验的归一化函数 (裁定 E); 函数签名接收并返回两区段, 以便与已有例外条目合并
- 新建条目继承该成员当前的 `platform_instance_id`, 身份绑定与组成员完全同构; 新建条目 `enabled: true`, `protect: false`
- **幂等**: 迁移后组已无 selected+excluded 组合, 重复调用不产生新条目 (读路径可能链式经过迁移, 必须保证)
- 迁移只在内存生效, 下次保存时落盘; 界面凭 `migrated_from_legacy` 提示"已将 N 条组排除迁移为成员例外, 保存后生效"
- 同组 overlap (included ∩ excluded) 迁移后: allow 留组, deny 到人, deny 优先, 有效行为不变
- 旧配置组没有 `protect` 字段, 归一化缺省 true, 保护行为与升级前完全一致, 无需迁移动作
- **语义差异须文档化** (裁定点 C, 已接受): 迁移按当前成员展开, 此后新加入该组的成员不再自动被否决。组级"跟成员走"的否决能力随本设计移除
- 迁移过程中的任何异常按开发规范 §3.2 隔离: 不得导致启动或设置页读取失败

## 6. 前端变更 (后端完成后实施)

- 组的插件卡片三态控件按模式收敛: `selected` → [不表态 | 允许]; `all` → [包含 | 排除]。控件抽成共享组件 (现位于 `AdministratorsPanel.tsx` 内部, 7-12 行 `stanceOptions` 与 220-245 行 radiogroup)
- 组卡片头部增加"账号保护"开关 (`protect`, 复用 `components/ui/Toggle.tsx`), 默认开; 例外条目卡片同款开关, 默认关
- 新增"成员例外"区块: 例外条目卡片 (启用开关 + 账号保护开关 + 平台选择 + 用户识别号 + 失效/重绑提示, 与组成员同款交互), 每卡片内对支持系统管理员的插件渲染同一三态控件 ([不表态 | 允许 | 排除])
- 「预览有效权限」按人展示来源归属, 例如 "group_admin: 允许 (组: 全体管理员), 否决 (例外) → 无效", 解决"算不清"问题
- `satrap-ui/src/api/administrators.ts`: `AdministratorGroup` 增 `protect`; 新增 `AdministratorOverride` 与 `AdministratorOverrideRebind`; `AdministratorSnapshot` 增 `overrides` / `invalid_overrides` / `migrated_from_legacy`; `save` 与 `preview` 载荷增 `overrides` / `rebind_overrides`; `AdministratorPreview` 增 `sources`
- `satrap-ui/src/pages/Settings/index.tsx` 的 `onSaved` (244-252 行): 除 `administrator_groups` 外同步 `administrator_overrides`, 原始 YAML 回写 (`yaml.dump({...parsed, administrator_groups, administrator_overrides})`) 必须一并带上, 否则切到"原始配置"页签保存会把例外层丢掉
- e2e `satrap-ui/e2e/administrators.mjs`: 增例外层, 保护开关与迁移提示的新用例 (含 `migrated_from_legacy` 提示文案断言)

## 7. 测试清单 (后端, 交接给 DeepSeek)

`normalize_administrator_overrides`:

- 重复身份 / 未知字段 / 空条目处理 (`protect: true` 保留, 否则丢弃) / enabled 与 protect 缺省与类型校验 / 实例绑定与重绑 (对齐现有组测试风格)
- **allow∩deny 重叠: 不报错, 记 error 日志, resolve 按否决优先** (裁定点 A)

`normalize_administrator_groups` 与迁移模式分离 (裁定 E):

- `strict_scope=True` 时 selected+excluded 抛 ValueError; `strict_scope=False` (缺省) 时不抛, 组合原样保留交给迁移
- 读路径 (from_dict / 设置页快照 / `AdministratorService.apply` / `apply_saved_administrator_groups`) 遇到 legacy 组合不抛异常, 且迁移产物正确落进例外层
- 迁移幂等: 对已迁移结果再调一次不产生新条目
- 迁移与已有例外条目合并: 同一个人既在 legacy 排除中又有手写例外时, deny 并入既有条目

`resolve`:

- 个人 allow 独立授予; 个人 deny 否决组允许; 组 all 模式排除与个人 deny 并存
- 停用 (`enabled: false`) 的组与例外均不命中; 实例代次不匹配则不命中; 来源归属字段正确

`protected_users`:

- 组 protect=false 后成员不再受保护; 例外 protect=true 的身份进入保护; 停用条目不保护; 实例失效不保护

preview:

- 例外-only 身份出现; sources 归属正确

修订:

- groups 或 overrides 任一变化, `section_revision` 变化; `snapshot()` 3 元组形态

`plugin_authorization` (裁定 F):

- 仅由例外层授权时 `source == "administrator_override"` 且 `override_ids` 非空, `group_ids` 为空
- 组与例外同时允许时 `source == "administrator_group"`, `override_ids` 仍记录
- 指纹区分组授予与例外授予 (同一权限由不同层授予产生不同 `permission_fingerprint`)

受影响的既有测试构造点 (按实际核验结果区分为"硬破"与"不需改"):

| 文件 | 行 | 核验结果 |
| --- | --- | --- |
| `tests/unit/test_administrator_groups.py` | 67 | **硬破**: `saved, revision = service.snapshot()` 2 元组解包 → 需改 3 元组 |
| `tests/unit/test_administrator_groups.py` | 72 | **静默失效 (必须改)**: `service.snapshot()[1] != revision` 改为 3 元组后下标 1 是 overrides 而非 revision, 比较变成"空列表 != 修订串"恒真, 断言恒过但不再验证任何东西; 必须显式改为下标 2 或解包三个值 |
| `tests/unit/test_administrator_groups.py` | 75 | 不需改: `snapshot()[0]` 仍是 groups |
| `tests/unit/test_administrator_groups.py` | 70/74/104 | 不需改: `apply` 的 overrides 有缺省 |
| `tests/unit/test_plugin_management_permissions.py` | 78 | **硬破**: `groups, _ = service.snapshot()` 需改 3 元组 |
| `tests/unit/test_plugin_management_permissions.py` | 49/80 | 不需改: 构造与 `apply` 用缺省 |
| `tests/unit/test_plugin_management_permissions.py` | 68 | 不需改: 组授权路径 `source` 仍为 `administrator_group` |
| `tests/unit/test_administrator_settings_api.py` | 71/94 | **硬破**: `administrator_revision` 变双区段必填 |
| `tests/unit/test_administrator_settings_api.py` | 39/43/70/78/103 | **硬破**: `save_administrator_groups` 增 keyword-only 必填 `overrides`, 旧位置调用直接 TypeError |
| `tests/unit/test_administrator_settings_api.py` | 31/33/49/53/57 | 不需改: `prepare_administrator_groups` 只处理组区段 |
| `tests/unit/test_administrator_plugin_integration.py` | 34/61/82 | 不需改: 构造 `overrides` 有缺省 |
| `tests/unit/test_administrator_plugin_integration.py` | 91/108/126/146/161 | 不需改: `apply` 的 overrides 有缺省 |
| `tests/benchmark/benchmark_plugin_hotpath.py` | 149/168 | **不需改**: 构造 `overrides` 有缺省, 空例外层下 `resolve` 结果与扫描路径不变, 各场景 `result_hash` 与 `tests/benchmark/results/plugin_hotpath/` 中基线一致 |

该表按"关键字缺省"签名测算; 若评审把 `apply` / 构造参数也改为必填, 上表"不需改"各行会转为硬破, 需同步补实参。

基准验收方式: 改后重跑 `benchmark_plugin_hotpath.py` 并用 `tests/benchmark/compare_results.py` 与已入库基线逐场景比对, 任一 `result_hash` 不一致即判定语义变化 (脚本非零退出)。重点看 `admin_groups_1/20/200` 与 `admin_groups_20` / `admin_groups_20_managed` 五个场景, 以及 `COUNTED` 中 `AdministratorService.snapshot` / `resolve` 的调用次数不变。

## 8. 裁定点 (2026-10-08 已全部定稿)

- **A. 个人层 allow∩deny 重叠**: 不做硬校验。前端三态控件从构造上杜绝重叠; 后端归一化检测到重叠时 `logger.error` 记录误配, 求值按否决优先, 不拒绝配置不中断启动
- **B. 账号保护**: 从隐式行为升级为显式开关。组新增 `protect` (缺省 true, 保持现行行为); 例外条目新增 `protect` (缺省 false); `protected_users()` 取两者并集
- **C. 迁移按当前成员展开, 新成员不继承否决**: 已接受, 迁移提示中明示
- **D. 例外层同时支持 allow 与 deny**: 已接受
- **E. 严格校验与迁移分离** (修订 2): 校验函数加 `strict_scope: bool = False` 关键字参数, 只控制规定模式下不允许的范围组合; 旧范围组合的展开逻辑放在独立迁移函数 `migrate_legacy_scope_exclusions`, 不塞进纯校验函数。写路径传 `strict_scope=True`, 读路径走迁移 (第 10 节逐点标注)。**备注**: 例外侧的 `strict_scope` 当前无对应拒绝规则 (唯一候选被裁定 A 排除), 保留仅为与组归一化同形, 评审可决定是否只留在组归一化上
- **F. 来源归属贯通到统一授权** (修订 2): `PermissionGrant` 增 `override_ids`, `source` 增 `administrator_override` 取值, 授权指纹与 debug 日志同步覆盖, 使"由哪一层授予"在持久申请复核与排障中可区分

## 9. 实施落点 (交接锚点)

| 文件 | 改动 |
| --- | --- |
| `satrap/core/config/administrator_groups.py` | 新增 `normalize_administrator_overrides`; `normalize_administrator_groups` 加 `strict_scope`; `AdministratorService` 双快照 (`__init__`/`apply`/`snapshot` 3 元组); `resolve` 扩展; `AdministratorGrant` 来源字段; `protected_users` 取 protect 并集; `administrator_revision(groups, overrides)` 覆盖双区段; 模块头文档随双区段职责更新 |
| `satrap/core/config/administrator_settings.py` | 快照/prepare/save/preview 携带 overrides; `prepare_administrator_overrides` 并列新增; `migrate_legacy_scope_exclusions`; `invalid_overrides` |
| `satrap/core/backend/BackendManager.py` **(修订 2 补)** | 144 行 `BackendConfig` 增 `administrator_overrides` 字段; 185-186 行 `from_dict` 读路径归一化+迁移; 212 行构造参数; 243 行 `AdministratorService(...)` 传双区段; 469-487 行 `apply_saved_administrator_groups` 读文档→迁移→双区段修订比对→`apply(groups, overrides)` |
| `satrap/core/config/document.py` **(修订 2 补)** | 347-350 行 `validate_config_document` 补 `administrator_overrides` 归一化 (迁移模式, 见第 10 节) |
| `satrap/core/config/loader.py` **(修订 2 补)** | 32-65 行 `default_config_document` 模板增加 `administrator_overrides` |
| `satrap/core/backend/control_server.py` | 管理员路由载荷透传 (1655/1662/1668/1672-1673/1679-1680/1684 行); 通用 `PUT /config` (1724-1736 行) 两区段过 `prepare` (严格) 并写回 `merged_config`; `POST /config/validate` (1765-1770 行) 走迁移模式 |
| `satrap/core/plugin_authorization.py` **(修订 2 改)** | `PermissionGrant` 增 `override_ids`; 132-134 行来源与 `administrator_override` 标签; 139-140 行指纹; 411-414 行日志 |
| `tests/unit/test_administrator_groups.py` | 第 7 节清单 |
| `docs/plugins/system-administrators.md` | 语义段与配置结构段改写 (三层模型, protect 开关, 例外区段, 迁移提示) |
| `docs/edictum/plugin-system.md` | 149 行"本地名单允许 OR 系统管理组授权"改为三层描述; 157 行 grants 来源列表增 `administrator_override` |
| `docs/README.md` | plugins/ 索引补一行本设计稿 (待实施规格) |
| `docs/archive/development/plugin-management-permission-contracts.md` | 第 6 节管理组规则补新一轮契约 (**本地留档, 该目录被 .gitignore 忽略, 不随仓库分发**) |
| `satrap-ui` | 第 6 节, 后端落地后另行实施 |

文档落点裁定: 本设计稿保留在 `docs/plugins/` 并在 `docs/README.md` 索引补一行 —— 它是待实施规格而非"只反映当时状态的过程记录", 归档约定不适用; 实施完成后规格并入 `system-administrators.md`, 设计稿本体再移归档。

## 10. 调用点读写标注表 (裁定 E 落实)

`normalize_administrator_groups` 同时位于读写两条路径, 因此每个内部调用点必须显式标注模式:

| 调用点 | 路径 | 模式 |
| --- | --- | --- |
| `BackendManager.BackendConfig.from_dict` (185-186 行) | 读·启动加载 | 迁移: 容忍 + `migrate_legacy_scope_exclusions` |
| `BackendManager.__init__` → `AdministratorService.__init__` (administrator_groups.py:215 内调 `apply`) | 读·运行时快照 | 迁移 |
| `AdministratorService.apply` (administrator_groups.py:227 归一化) | 读·快照替换 | 迁移 (幂等, 可重复经过) |
| `BackendManager.apply_saved_administrator_groups` (469-487 行) | 读·热应用 | 迁移 |
| `administrator_settings.administrator_settings_snapshot` (55 行) | 读·设置页 | 迁移 (+ 报 `migrated_from_legacy`) |
| `administrator_settings.prepare_administrator_groups` (96 行) | 写 | `strict_scope=True` |
| `administrator_settings.prepare_administrator_overrides` (新增) | 写 | `strict_scope=True` |
| `administrator_settings.save_administrator_groups` (99-116 行, 经 prepare) | 写 | 严格 (继承 prepare) |
| `control_server` 通用 `PUT /config` (1731-1735 行) | 写 | 严格: 两区段都过 prepare 并写回 `merged_config` |
| `control_server` `POST /config/validate` (1765-1770 行) 与 `document.validate_config_document` (347-350 行) | 读/预检 | 迁移 (只校验可加载性并回带规范化结果, 不让 CLI `config validate` 对存量配置误报) |
| `cli.cmd_config` validate / set (112/132-133 行) | 读/写 | 经 `validate_config_document` → 迁移; 后续保存自动落盘迁移结果 |
| `preview_administrator_groups` (131 行, 经 prepare) | 写·预览 | 严格 (继承 prepare) |

补充约束:

- `validate_config_document` 目前调用 `BackendConfig.from_dict` 只为解析校验 (349 行), `from_dict` 的结果不会写回 `normalized`; 补 overrides 归一化时必须显式写回 `normalized["administrator_overrides"]`, 否则手工编辑的非法例外会被静默写盘
- 迁移函数在写路径不会被调用 (严格模式已拒绝 selected+excluded), 因此"保存后 migrated_from_legacy 变为 false"自然成立

## 11. 建议实施批次

1. B1 归一化与求值: `normalize_administrator_overrides`、`strict_scope`、`migrate_legacy_scope_exclusions`、双快照、`resolve`、`protected_users`、`administrator_revision` + 第 7 节单元测试
2. B2 落盘与接口: `administrator_settings.py`、`BackendManager.py`、`document.py`、`loader.py`、`control_server.py` + 第 7 节接口测试
3. B3 授权来源: `plugin_authorization.py` (裁定 F) + 对应断言
4. B4 文档: `system-administrators.md`、`plugin-system.md`、`README.md` 索引
5. B5 前端: 第 6 节 (后端落地后另行实施)

## 12. 实施记录 (2026-10-08 落地)

五个批次全部完成, 均按第 10 节标注表区分严格模式与迁移模式:

- **关键字参数一律 keyword-only**: `save_administrator_groups(path, groups, *, overrides, expected_revision, ...)` 与 `preview_administrator_groups(config, groups, *, overrides, ...)`。这一条来自计划外追加的 refinement: 位置参数加缺省会让旧式调用 `save(path, draft(), rev)` 把修订串绑到 `overrides` 上静默放行, keyword-only 让同一调用立即 `TypeError`, 误绑窗口从"静默丢数据"降为"显式报错"。落地时补了一条测试固化该契约, 并在测试里用 `cast(Any, ...)` 显式构造越界调用, 使 pyright 不把它计为真实类型错误
- **`administrator_revision(groups, overrides)` 与 `apply(groups, overrides=None)`**: 前者必填 (参与热应用修订比对, 可缺省会算出忽略例外层的半个修订), 后者保留缺省 (生产调用方只有两处且都在本改动内)。`apply` 的缺省值实现为 `None` 并在内部折成空列表, 避免元组被列表校验拒绝
- **裁定 F 的连带影响**: 组的 `included` 命中与例外的 `allow` 命中同时存在时, `source` 取 `administrator_group` (组优先标记), 但 `override_ids` 仍记录例外来源, 保证归属信息不丢
- **`strict_scope` 在例外侧**: 归一化函数保留同形参数但当前不启用任何拒绝规则 (唯一候选 allow∩deny 被裁定 A 排除), 已按第 8 节备注保留待评审
- **迁移函数无副作用地幂等**: 组已无 selected+excluded 组合时直接返回输入副本, 读路径链式经过不会累积条目
- **前端三态控件抽为共享组件**: `StanceControl` 与 `PluginStanceList` 同时服务于组卡片与例外卡片, 由 `options` 参数区分: 组按模式收敛 (selected 只给 [不表态|允许]), 例外恒定提供 [不表态|允许|排除]
- **前端已实现迁移提示、protect 开关、例外区块与来源归属**: `migrated_from_legacy` 提示在保存后消失; `Settings/index.tsx` 的 `onSaved` 把两个区段同步进原始 YAML 文本, 避免切到原始配置页签保存时丢失例外层
- **既有测试调用点按核验结果修正**: 硬破 4 组 (snapshot 三元组解包、`administrator_revision` 双区段、`save_administrator_groups` 补关键字实参) 全部更新; 其中 `test_administrator_groups.py` 的 `snapshot()[1] != revision` 属计划标注的"断言静默失效", 已改为显式解包三元组。既有断言语义未改, 唯一行为断言调整是把组级排除的用例改为 all 模式 (指定插件模式下的组排除现在按设计迁移到成员例外)

验证结果: pytest 3512 passed / 23 skipped (仅 `test_log_retention.py` 两条 Windows 符号链接与 powershell 环境限制用例失败, 已用 stash 复核为改动前既有问题); pyright 297 errors 与改动前基线完全一致, 0 新增, 受影响文件 0 error; 插件热路径基准 16 个场景 `result_hash` 与改动前逐项一致 (A/B 用 stash 对比), 调用计数无回归; 前端 tsc 0 error / vitest 238 passed / 20 个 e2e 全过 / eslint 无新增告警。
