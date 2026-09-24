# 语音附件转写方案 (issue #10 补充, 2026-09-22, 修订 2)

## 事实核查 (SnowLuma 官方文档 + 本机 1.14.17 发行包源码)

文档 (snowluma.github.io) 没有写 record 段的编码; 以下来自 `index.mjs` / `config-GJCFWjtq.js` 直接阅读:

1. **上报的 record 段只有 `file` 与 `url` 两个字段**, `file` 是文件名或 fileId, `url` 是 QQ 媒体服务器直链 (`record.toSegment`)。文件名由 `element.fileName || \`${md5Hex}.amr\`` 生成, **后缀 `.amr` 是兜底名, 不代表内容编码**。
2. **原始内容是 SILK v3** (`voiceFormat`, `#!SILK_V3` 签名, 转码模块注释 "SILK→WAV/FLAC is a decompressing direction")。QQ AI 语音是 `0x03` 容器变体, SnowLuma 会归一化。因此 Satrap 直接下载 `url` 得到的字节, ffmpeg/PyAV **无法解码** (无 SILK 解码器), 之前"用 av 解 amr"的方案在真实 QQ 语音上不成立。
3. SnowLuma 提供两条服务端能力, 都是 OneBot 扩展动作:
   - `get_record {file, out_format}`: 用其**内置的 SILK 版 ffmpeg addon** 把语音转成 `mp3/amr/wma/m4a/spx/ogg/wav/flac` 之一, 响应 `{file, url, file_size, file_name, out_format, base64}`。上限 64 MiB 输入 / 256 MiB 输出。这与 NapCat 的 `get_record` 同构。
   - `fetch_ptt_text {message_id}` (别名 `get_ptt_text`/`get_record_text`): 走 QQ 官方 `pttTrans` 协议做语音转文字, 返回 `{text}`, 20 秒等待。不需要任何第三方 ASR。
4. 结论: **Satrap 不需要自己解 SILK, 也不需要 `av`/`pilk` 依赖**。转码应委托给 OneBot 实现 (SnowLuma/NapCat 都支持 `get_record out_format`), 本地转码只作为"其他 OneBot 实现给了 ffmpeg 可解格式 (amr/mp3/ogg) 直链"的可选兜底。

## 目标

1. 真实 QQ 群语音进入已配置的 ASR 转写 (主路径: `get_record out_format=wav` → 已配置 ASR)
2. 提供零依赖的 QQ 原生转写作为可选来源 (`fetch_ptt_text`), 用户没配 ASR 也能用
3. 不新增硬依赖; `av` 降为可选兜底, 未装时行为清晰
4. 所有路径遵守现有出站预算与降级标记; 未唤醒消息不触发任何动作

## 设计

### 语音来源策略 `voice_transcribe` (平台设置, 默认 `"asr"`)

| 值 | 行为 |
|---|---|
| `"off"` | 不转写, 标记 `[语音: 未启用转写]` (现有 disabled 语义) |
| `"asr"` | 需 `asr_model`; 获取 wav 后送已配置 ASR (默认) |
| `"platform"` | 调 `fetch_ptt_text`, 用 QQ 原生转写, 不需要 `asr_model` |
| `"asr_then_platform"` | ASR 失败/未配置时回退 `fetch_ptt_text` |

### 音频获取 (`"asr"` 路径) 三级顺序, 在 `attachments.py` 新增 `_fetch_voice_wav(event, comp) -> tuple[bytes, str]`:

1. **平台转码** (新, 主路径): `adapter.admin` 新增 `get_record(file, out_format="wav") -> bytes`, 走现有 `OneBotAdmin._call` (超时/UnsupportedAdminAction/AdminActionRejected 语义复用)。retcode 10002/1404 (实现不支持该动作) 记 `unsupported` 后进入第 2 级; 其他失败直接 `failed`。返回的 base64 解码后检查 `RIFF` 头与 `AUDIO_MAX_BYTES`。
2. **直链下载** (现有): `_download(url)` → `probe_audio` (magic bytes)。已是 wav/ogg/mp3/flac/m4a 等 ASR 直接接受的格式则原样送; 是 SILK (`#!SILK_V3` / `\x02#!SILK_V3` / `\x03#!SILK_V3`) 则标记 `unsupported/silk_needs_platform_transcode` (提示用户 OneBot 实现需支持 `get_record out_format`)。
3. **本地转码** (正式支持的第三级, 面向非 SnowLuma 实现): magic 为 ffmpeg 可解码的编码 (`#!AMR`/`#!AMR-WB`, 以及 ASR 接口不直接接受的其他容器) 且 `av` 可导入时, 用 PyAV 解码并重采样为 16 kHz 单声道 wav (已实测 4 秒 amr 5.5 ms), 在 `EXTRACT_WORKERS` 线程池执行, 时长上限 `AUDIO_MAX_SECONDS=300`。`av` 未装标记 `unsupported/av_missing`, 提示安装 `pip install av`。适用场景: go-cqhttp/Lagrange/NapCat 未开 `get_record` 转码但给出 amr/mp3 直链的部署; SILK 裸流仍不在此级范围 (ffmpeg 无 SILK 解码器)。

### `"platform"` 路径

`adapter.admin.fetch_ptt_text(message_id) -> str`, 用事件 `call_origin.source_message_id`; 20 秒超时与 `ASR_TIMEOUT` 取小。结果同样冻结到 `Record.text`, 投影为 `[语音 转写内容: …]`, `AttachmentResult.reason="platform_ptt"` 供诊断区分来源。

### 模块划分

- `pipeline/audio_convert.py` (新, 小): `probe_audio(data, suffix) -> AudioProbe(codec, accepted, reason)` + `convert_amr_to_wav(data, max_seconds)` (惰性导入 av)。只做纯函数, 不碰事件/网络。
- `platform/onebot/admin.py`: 加 `get_record` 与 `fetch_ptt_text` 两个只读动作, 加进 `ADMIN_CAPABILITIES` 矩阵; 不暴露为 group_admin 工具 (它们是管线内部能力, 不给模型调)。
- `pipeline/attachments.py`: record 分支改为按 `voice_transcribe` 分派; 删除下载前的后缀白名单 (改为 magic 探测后判断)。
- `config/platform_policy.py`: `voice_transcribe` 枚举校验; 加入 `hot_keys`。
- `config/model_service.test_asr_config`: 控制面测试端点对上传音频也走 `probe_audio` + amr 本地转码 (无平台可用), SILK 明确报"请在群内用 get_record 路径"。
- 前端 Platforms 表单: `asr_model` 旁加下拉 "语音转写来源" 四选项; `adminMigration` 默认 `asr`; e2e 断言。
- 依赖: `pyproject [project.optional-dependencies] audio = ["av>=15,<19"]`; 文档说明 SnowLuma 用户不需要, 其他 OneBot 实现用户建议安装。

### 预算与安全

- `get_record` 响应 base64 解码后 > `AUDIO_MAX_BYTES` (16 MiB) 拒绝; wav 16k 单声道 16 MiB ≈ 8.7 分钟, 对群语音足够
- `fetch_ptt_text` 与 `get_record` 都受 `OutboundTurns` 之外的 admin `_call` 超时 (现有 15 秒) 约束; `fetch_ptt_text` 需单独传 25 秒 (SnowLuma 内部等待 20 秒)
- 每事件附件数上限 4 不变; 平台动作按附件逐个串行, 不并发轰炸 OneBot 实现
- 两个新动作只在事件已唤醒且通过限流后调用 (现有 Step.4 位置), 未唤醒不触发

### 测试

- `test_audio_convert.py` (新): probe 6 组 (wav/ogg/mp3/amr/silk 三变体/未知); amr→wav (`importorskip("av")`, 现场用 av 编码正弦波作样本); av 缺失分支
- `test_attachments.py` 追加: `get_record` 成功→ASR 收到 wav; `get_record` 返回 1404→回退直链→SILK 标记; `voice_transcribe=platform` 走 `fetch_ptt_text` 且不调 ASR; `asr_then_platform` ASR 失败回退; `off`; 策略校验
- `test_onebot_admin.py` 追加: 两个动作的参数与响应结构断言, base64 超限拒绝
- 前端 vitest/e2e 各加断言
- 基准: `benchmark_platform_ingress.py` 加 `audio_probe` (magic 探测 2000 次) 与 `amr_convert` (若 av 可用) 两场景

### 真实验收 (用 `.toolkit` 与本机 SnowLuma)

1. `get_record out_format=wav` 真实调用: SnowLuma 探针脚本扩展一个 record 缓存替身 + 真实 `convertAudioBytes`? — **不可行**, 探针只加载网络模块不加载 QQ 桥, 无真实 SILK 样本。改为: 单测用 SnowLuma 源码里的 `#!SILK_V3` 签名字节验证探测分支; `get_record` 动作用 AsyncMock 返回一段真实 wav 的 base64, 再送真实 ASR (复用 P4 脚本形态)。
2. 在验收记录中明确: 真实 SILK→wav 转码由 SnowLuma 内置 ffmpeg 完成, 本轮无法在不登录 QQ 的前提下端到端验证, 属方案裁定的"QQ 侧模拟"范围。

## 与修订 1 的差异

- 主路径从"本地 PyAV 解 amr"改为"委托 OneBot 实现 `get_record out_format=wav`": 因为真实内容是 SILK, PyAV 解不了; SnowLuma/NapCat 都自带 SILK 版 ffmpeg
- 新增 `fetch_ptt_text` 零依赖来源
- `av` 定位为第三级本地转码 (面向非 SnowLuma 实现) 与控制面测试端点, 可选安装; SnowLuma 场景不依赖它
- 不再考虑 `pilk`

## 实施顺序 (单批)

1. `audio_convert.py` + 单测
2. `admin.py` 两动作 + 单测
3. `attachments.py` 分派 + 策略 + 单测
4. `model_service` 测试端点 + 前端
5. pyproject extras + `../../platform/platforms.md` / `configuration.md` / `config.example.yaml` / progress
6. 真实 ASR 验收 (wav base64 替身 → 真实 ASR) + benchmark after

对外行为变化: 已配置 `asr_model` 的 OneBot 实例, 群语音从"不支持的格式"变为经 `get_record` 转码后转写, 每条语音多一次 OneBot 动作调用与一次 ASR 调用; 可用 `voice_transcribe: off` 关闭。

## 验收记录 (2026-09-22)

- 单测: `tests/unit` 1813 passed / 19 skipped; 新增 `test_audio_convert.py` 14 例, `test_onebot_admin.py` +4, `test_attachments.py` +5, `test_model_config_service.py` +1
- pyright: 改动文件 0 error, warning 数与基线一致 (`test_attachments.py` 2 处为既有 lambda warning)
- 前端: tsc 0, eslint 0, vitest 69, `test:e2e:platform` 通过 (新增 voice_transcribe 默认不写入 / 选择后写入断言)
- 真实 ASR (硅基流动 transcriptions, 密钥仅运行时读取): ① `get_record` 替身返回 2 秒 16 kHz wav → `resolved`; ② `get_record` 返回 1404 → 下载 amr → PyAV 本地转码 → `resolved`; 样本为合成正弦, 转写文本为空属预期, 验证的是三级路径与服务接受转码结果
- 基准 `tests/benchmark/benchmark_audio_attachments.py` → `results/platform/audio.json` (50 次 × 3 轮取中位): `audio_probe` 1.1 µs; `amr_convert` 10 秒 AMR-NB → 16 kHz wav 16.8 ms; `record_get_record` 全路径 0.9 ms; `record_local_amr` 全路径 17.1 ms (转码在线程池, 不阻塞事件循环)
- 未验证: SnowLuma 真实 SILK → wav (`get_record` 内部转码需 QQ 登录), 依据为 bundle 源码阅读
- 依赖声明落在 `setup.py` extras (`pip install -e .[audio]`), 仓库无 `[project]` 表, 方案中 pyproject 表述据此修正
