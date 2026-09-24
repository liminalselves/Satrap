"""durability 清单组件: 结构校验, 额外键与降级语义, 先标记后隔离顺序与隔离扫描

覆盖反例:
- 版本非法与 expected_files 状态非法必须拒读, 不被解释为空库
- 降级标记写失败时不得隔离坏文件, 原文件字节保留
- 隔离失败只记录日志, 不把异常抛给调用方
- 冻结两类存储的清单样本, 对比接受/拒绝, 归一化结果与写回时的额外键语义
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
import json

import pytest

from satrap.core.pipeline.manual_wake_store import MANIFEST_SPEC as STORE_SPEC, ManualWakeStore
from satrap.core.platform.onebot.request_registry import (
    LEDGER_MANIFEST_SPEC as LEDGER_SPEC,
    RequestApprovalLedger,
)
from satrap.core.storage import durability as durability_module
from satrap.core.storage.durability import (
    DurabilityManifest,
    Manifest,
    ManifestSpec,
    validate_manifest,
)

SAMPLE_SPEC = ManifestSpec(
    version=1,
    expected_files={"main": frozenset({"present", "missing"})},
    keep_extra_keys=False,
    degraded_requires_reason=True,
    normalize_degraded_at=True,
)


def _payload(**overrides: Any) -> dict[str, Any]:
    """一份合法清单, 按需覆写字段"""
    base: dict[str, Any] = {
        "version": 1,
        "initialized_at": 100.0,
        "updated_at": 200.0,
        "expected_files": {"main": "present"},
        "degraded": None,
    }
    base.update(overrides)
    return base


def _store_payload(**overrides: Any) -> dict[str, Any]:
    """存储清单样本: main 与 archive 都必须声明"""
    payload = _payload()
    payload["expected_files"] = {"main": "present", "archive": "absent"}
    payload.update(overrides)
    return payload


def _manifest(payload: object, spec: ManifestSpec = SAMPLE_SPEC) -> Manifest:
    return validate_manifest(payload, spec)


def _harness(
    tmp_path: Path,
    spec: ManifestSpec = SAMPLE_SPEC,
    validate: Callable[[object], Manifest] | None = None,
) -> tuple[DurabilityManifest, list[str]]:
    """构造组件与事务进入记录, 便于断言事务边界"""
    entered: list[str] = []

    @contextmanager
    def transaction() -> Iterator[None]:
        entered.append("enter")
        yield
        entered.append("exit")

    component = DurabilityManifest(
        tmp_path / "store.json", spec,
        transaction=transaction,
        validate=validate if validate is not None else (lambda raw: validate_manifest(raw, spec)),
        log_prefix="[Test]",
    )
    return component, entered


class TestStructureValidation:
    def test_missing_manifest_file_returns_none(self, tmp_path: Path):
        component, _ = _harness(tmp_path)
        assert component.read() is None

    def test_version_mismatch_rejected(self):
        with pytest.raises(ValueError, match="版本"):
            _manifest(_payload(version=2))

    def test_non_object_and_missing_expected_files_rejected(self):
        with pytest.raises(ValueError, match="对象"):
            _manifest(["not", "a", "dict"])
        with pytest.raises(ValueError, match="expected_files"):
            _manifest(_payload(expected_files=None))

    def test_expected_state_and_required_keys(self):
        # 未声明的状态与缺失的必需键都属于清单损坏
        with pytest.raises(ValueError, match="文件状态"):
            _manifest(_payload(expected_files={"main": "corrupt"}))
        with pytest.raises(ValueError, match="文件状态"):
            _manifest(_payload(expected_files={}))
        # 声明的键之外的 expected_files 键按声明忽略
        manifest = _manifest(_payload(expected_files={"main": "missing", "extra": "whatever"}))
        assert manifest.expected == {"main": "missing"}

    def test_time_fields_must_be_numbers(self):
        with pytest.raises(ValueError, match="时间字段"):
            _manifest(_payload(updated_at="200"))
        with pytest.raises(ValueError, match="时间字段"):
            _manifest(_payload(initialized_at=True))

    def test_read_round_trips_file(self, tmp_path: Path):
        component, _ = _harness(tmp_path)
        (tmp_path / "store.manifest.json").write_text(
            json.dumps(_payload(expected_files={"main": "missing"})), encoding="utf-8",
        )
        manifest = component.read()
        assert manifest is not None
        assert manifest.expected == {"main": "missing"}
        assert manifest.initialized_at == 100.0

    def test_validator_hook_runs_business_cross_check(self, tmp_path: Path):
        # 业务方在薄层里追加交叉校验 (存储要求未降级时主文件必须 present)
        def cross_checked(raw: object) -> Manifest:
            manifest = validate_manifest(raw, SAMPLE_SPEC)
            if manifest.degraded is None and manifest.expected["main"] != "present":
                raise ValueError("清单声明主文件缺失但未降级")
            return manifest

        component, _ = _harness(tmp_path, SAMPLE_SPEC, cross_checked)
        (tmp_path / "store.manifest.json").write_text(
            json.dumps(_payload(expected_files={"main": "missing"})), encoding="utf-8",
        )
        with pytest.raises(ValueError, match="未降级"):
            component.read()

    def test_broken_json_raises_decode_error(self, tmp_path: Path):
        (tmp_path / "store.manifest.json").write_text("not-json", encoding="utf-8")
        component, _ = _harness(tmp_path)
        with pytest.raises(json.JSONDecodeError):
            component.read()


class TestExtraKeyAndDegradedSemantics:
    def test_extra_keys_dropped_when_spec_disallows(self):
        manifest = _manifest(_payload(extra_key="keep-me", expected_files={"main": "present", "extra": "x"}))
        assert "extra_key" not in manifest.payload()
        assert manifest.payload()["expected_files"] == {"main": "present"}

    def test_extra_keys_kept_when_spec_allows(self):
        spec = ManifestSpec(version=1, expected_files={"entries": frozenset({"present"})}, keep_extra_keys=True)
        manifest = _manifest({"version": 1, "initialized_at": 1.0, "updated_at": 2.0,
                              "expected_files": {"entries": "present"}, "degraded": None, "note": {"a": 1}}, spec)
        assert manifest.payload()["note"] == {"a": 1}
        # 额外键不影响声明字段的归一化结果
        assert manifest.expected == {"entries": "present"}

    def test_degraded_reason_strictness(self):
        with pytest.raises(ValueError, match="降级原因"):
            _manifest(_payload(degraded={"reason": "", "at": 1.0}))
        with pytest.raises(ValueError, match="降级原因"):
            _manifest(_payload(degraded={"at": 1.0}))
        with pytest.raises(ValueError, match="降级字段"):
            _manifest(_payload(degraded="broken"))
        lenient = ManifestSpec(version=1, expected_files={"entries": frozenset({"present"})},
                               keep_extra_keys=True, degraded_requires_reason=False, normalize_degraded_at=False)
        manifest = _manifest({"version": 1, "initialized_at": 1.0, "updated_at": 2.0,
                              "expected_files": {"entries": "present"}, "degraded": {"reason": "", "at": "raw"}}, lenient)
        assert manifest.degraded is not None
        assert manifest.degraded.reason == ""
        # 归一化视图不解释 at, 写回时按原值写回而不是替换成 0
        assert manifest.degraded.at == 0.0
        assert manifest.payload()["degraded"] == {"reason": "", "at": "raw"}

    def test_degraded_at_normalized_when_spec_requires(self):
        assert _manifest(_payload(degraded={"reason": "x"})).degraded is not None
        manifest = _manifest(_payload(degraded={"reason": "x", "at": "12"}))
        assert manifest.degraded is not None and manifest.degraded.at == 12.0
        # 非数字的 at 按清单损坏处理, 与迁移前的行为一致
        with pytest.raises(ValueError):
            _manifest(_payload(degraded={"reason": "x", "at": "not-a-number"}))

    def test_mark_degraded_sets_marker_without_touching_expected(self, tmp_path: Path):
        component, _ = _harness(tmp_path)
        manifest = component.fresh(initialized_at=10.0, expected={"main": "present"})
        written: list[Manifest] = []
        outcome = component.mark_degraded(manifest, reason="main_corrupt", persist=written.append)
        assert outcome.persisted is True
        assert outcome.manifest.degraded is not None
        assert outcome.manifest.degraded.reason == "main_corrupt"
        assert outcome.manifest.expected == {"main": "present"}
        assert written and written[0].payload()["degraded"]["reason"] == "main_corrupt"


class TestDegradeOrdering:
    def test_marker_failure_keeps_original_file(self, tmp_path: Path):
        component, _ = _harness(tmp_path)
        bad = tmp_path / "store.json"
        bad.write_text("not-json{", encoding="utf-8")

        def broken_persist(manifest: Manifest) -> None:
            raise OSError("disk full")

        outcome = component.degrade_then_quarantine(
            component.fresh(initialized_at=1.0, expected={"main": "present"}),
            reason="main_corrupt", persist=broken_persist, bad_files=[bad],
        )
        assert outcome.persisted is False
        assert outcome.quarantined == ()
        assert bad.is_file() and bad.read_text(encoding="utf-8") == "not-json{"
        assert list(tmp_path.glob("store.json.corrupt-*")) == []
        # 内存清单仍带降级标记, 调用方据此保持降级
        assert outcome.manifest.degraded is not None

    def test_timeout_during_marker_keeps_original_file(self, tmp_path: Path):
        component, _ = _harness(tmp_path)
        bad = tmp_path / "store.json"
        bad.write_text("not-json{", encoding="utf-8")

        def timeout_persist(manifest: Manifest) -> None:
            raise TimeoutError("lock busy")

        outcome = component.degrade_then_quarantine(
            component.fresh(initialized_at=1.0, expected={"main": "present"}),
            reason="main_corrupt", persist=timeout_persist, bad_files=[bad],
        )
        assert outcome.persisted is False and bad.is_file()

    def test_marker_written_before_quarantine(self, tmp_path: Path):
        component, entered = _harness(tmp_path)
        bad = tmp_path / "store.json"
        bad.write_text("not-json{", encoding="utf-8")
        order: list[str] = []

        def persist(manifest: Manifest) -> None:
            order.append("marker")
            assert bad.is_file(), "标记必须先于隔离"

        outcome = component.degrade_then_quarantine(
            component.fresh(initialized_at=1.0, expected={"main": "present"}),
            reason="main_corrupt", persist=persist, bad_files=[bad],
        )
        order.append("after")
        assert order == ["marker", "after"]
        assert outcome.persisted is True
        assert [item.original for item in outcome.quarantined] == [bad]
        assert not bad.is_file()
        # 标记与隔离各进一次事务
        assert len(entered) == 4

    def test_quarantine_failure_does_not_raise(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        component, _ = _harness(tmp_path)
        bad = tmp_path / "store.json"
        bad.write_text("not-json{", encoding="utf-8")

        def broken_quarantine(path: Path) -> Path:
            raise OSError("rename failed")

        monkeypatch.setattr(durability_module, "quarantine_file", broken_quarantine)
        outcome = component.degrade_then_quarantine(
            component.fresh(initialized_at=1.0, expected={"main": "present"}),
            reason="main_corrupt", persist=lambda manifest: None, bad_files=[bad],
        )
        assert outcome.persisted is True and outcome.quarantined == ()
        assert bad.is_file()

    def test_quarantine_preserves_bytes_and_scan_ignores_manifest_corruptions(self, tmp_path: Path):
        component, _ = _harness(tmp_path)
        bad = tmp_path / "store.json"
        bad.write_text("not-json{", encoding="utf-8")
        outcome = component.degrade_then_quarantine(
            component.fresh(initialized_at=1.0, expected={"main": "present"}),
            reason="main_corrupt", persist=lambda manifest: None, bad_files=[bad],
        )
        item = outcome.quarantined[0]
        assert item.target.name.startswith("store.json.corrupt-")
        assert item.target.read_text(encoding="utf-8") == "not-json{"
        # 扫描只认数据文件的隔离件, 清单自己的隔离件不算目录曾初始化
        (tmp_path / "store.manifest.json.corrupt-20250101-000000").write_text("{}", encoding="utf-8")
        assert component.quarantine_names() == [item.target.name]


class TestFrozenManifestSamples:
    """两类存储的旧清单样本: 迁移前后必须给出同一结论

    样本取自迁移前的读取路径 (manual_wake_store._validate_manifest 与
    request_registry._read_manifest) 及其测试覆盖的组合
    """

    def test_store_samples(self):
        manifest = _manifest(_store_payload(), STORE_SPEC)
        assert manifest.expected == {"main": "present", "archive": "absent"}
        assert manifest.degraded is None
        # 未降级却声明主文件缺失: 本组件放行, 业务交叉校验拒绝
        contested = _manifest(_store_payload(expected_files={"main": "missing", "archive": "absent"}), STORE_SPEC)
        assert contested.degraded is None
        assert contested.expected == {"main": "missing", "archive": "absent"}
        # 版本非法, 缺键, 非法状态, 空原因都拒读
        with pytest.raises(ValueError):
            _manifest(_store_payload(version=2), STORE_SPEC)
        with pytest.raises(ValueError):
            _manifest(_store_payload(expected_files={"archive": "absent"}), STORE_SPEC)
        with pytest.raises(ValueError):
            _manifest(_store_payload(expected_files={"main": "present", "archive": "gone"}), STORE_SPEC)
        with pytest.raises(ValueError):
            _manifest(_store_payload(degraded={"reason": "", "at": 1.0}), STORE_SPEC)
        # 降级清单的额外键按存储语义丢弃, 归档三态保留
        degraded = _manifest(
            _store_payload(expected_files={"main": "missing", "archive": "missing"},
                           degraded={"reason": "main_corrupt", "at": 3.0, "note": "drop"}),
            STORE_SPEC,
        )
        assert degraded.payload() == {
            "version": 1, "initialized_at": 100.0, "updated_at": 200.0,
            "expected_files": {"main": "missing", "archive": "missing"},
            "degraded": {"reason": "main_corrupt", "at": 3.0},
        }

    def test_ledger_samples(self):
        sample: dict[str, Any] = {"version": 1, "initialized_at": 1.0, "updated_at": 2.0,
                                  "expected_files": {"entries": "present"}, "degraded": None, "history": ["kept"]}
        manifest = _manifest(sample, LEDGER_SPEC)
        assert manifest.expected == {"entries": "present"}
        # 账本保留历史额外键: 无改动时写回与读入逐键一致
        assert manifest.payload() == sample
        lenient = _manifest({**sample, "degraded": {"reason": "", "at": "raw", "note": "x"}}, LEDGER_SPEC)
        assert lenient.degraded is not None and lenient.degraded.reason == ""
        # 降级原因允许空串, 不解释 at, 嵌套额外键按原值写回
        assert lenient.payload() == {**sample, "degraded": {"reason": "", "at": "raw", "note": "x"}}
        # 账本要求 entries 恒为 present: 其它取值仍是清单损坏
        with pytest.raises(ValueError):
            _manifest({**sample, "expected_files": {"entries": "missing"}}, LEDGER_SPEC)
        with pytest.raises(ValueError):
            _manifest({**sample, "degraded": {"reason": None}}, LEDGER_SPEC)
        with pytest.raises(ValueError):
            _manifest({**sample, "version": 2}, LEDGER_SPEC)

    def test_ledger_snapshot_write_back_keeps_all_extra_keys(self):
        """旧清单完整写回: 顶层, expected_files 与 degraded 的额外键都要原样保留"""
        sample: dict[str, Any] = {
            "version": 1, "initialized_at": 1.0, "updated_at": 2.0,
            "expected_files": {"entries": "present", "future": "keep"},
            "degraded": {"reason": "", "at": "raw", "note": "keep"},
            "history": ["kept"],
        }
        manifest = _manifest(sample, LEDGER_SPEC)
        assert manifest.payload() == sample
        # 声明键仍以归一化视图为准, 额外键不参与判定
        assert manifest.expected == {"entries": "present"}
        assert manifest.degraded is not None and manifest.degraded.at == 0.0

    def test_active_marker_replaces_nested_payload_only(self):
        """主动写入新降级标记时按旧行为整体替换标记, 其它位置的额外键不受影响"""
        sample: dict[str, Any] = {
            "version": 1, "initialized_at": 1.0, "updated_at": 2.0,
            "expected_files": {"entries": "present", "future": "keep"},
            "degraded": {"reason": "old", "at": 5.0, "note": "drop"},
            "history": ["kept"],
        }
        payload = _manifest(sample, LEDGER_SPEC).with_degraded("probe", 9.0).payload()
        assert payload["degraded"] == {"reason": "probe", "at": 9.0}
        assert payload["expected_files"] == {"entries": "present", "future": "keep"}
        assert payload["history"] == ["kept"]


class TestFreshManifest:
    def test_fresh_payload_matches_legacy_skeleton(self, tmp_path: Path):
        component, _ = _harness(tmp_path)
        manifest = component.fresh(initialized_at=5.0, expected={"main": "present"}, updated_at=6.0)
        assert manifest.payload() == {
            "version": 1, "initialized_at": 5.0, "updated_at": 6.0,
            "expected_files": {"main": "present"}, "degraded": None,
        }
        assert manifest.version == 1

    def test_with_expected_and_updated_at_keep_degraded(self, tmp_path: Path):
        component, _ = _harness(tmp_path)
        manifest = component.fresh(initialized_at=5.0, expected={"main": "present"})
        marked = manifest.with_degraded("main_corrupt", 7.0)
        updated = marked.with_expected({"main": "missing"}).with_updated_at(8.0)
        assert updated.degraded is not None and updated.degraded.at == 7.0
        assert updated.updated_at == 8.0
        assert updated.payload()["expected_files"] == {"main": "missing"}
        # 原清单不被就地改动
        assert manifest.degraded is None and manifest.expected == {"main": "present"}

    def test_payload_does_not_alias_declared_fields(self):
        spec = ManifestSpec(version=1, expected_files={"entries": frozenset({"present"})}, keep_extra_keys=True)
        source: dict[str, Any] = {"version": 1, "initialized_at": 1.0, "updated_at": 2.0,
                                  "expected_files": {"entries": "present"}, "degraded": {"reason": "x", "at": 1.0}}
        manifest = _manifest(source, spec)
        payload = cast(dict[str, Any], manifest.payload())
        payload["expected_files"]["entries"] = "missing"
        payload["degraded"]["reason"] = "rewritten"
        assert manifest.expected == {"entries": "present"}
        assert manifest.degraded is not None and manifest.degraded.reason == "x"


class TestStorageDecisions:
    """用冻结清单样本驱动两类存储, 固定迁移后的接受/拒绝与降级结论"""

    @staticmethod
    def _store(tmp_path: Path) -> ManualWakeStore:
        return ManualWakeStore(tmp_path / "store.json")

    @staticmethod
    def _write_manifest(tmp_path: Path, payload: dict[str, Any]) -> None:
        (tmp_path / "store.manifest.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_store_manifest_with_extra_key_and_reason_is_accepted(self, tmp_path: Path):
        store = self._store(tmp_path)
        store.accept_request("bot", "r1", "fp", "group:20", "op")
        self._write_manifest(tmp_path, {
            "version": 1, "initialized_at": 1.0, "updated_at": 2.0,
            "expected_files": {"main": "present", "archive": "absent"},
            "degraded": {"reason": "main_corrupt", "at": 3.0, "note": "旧版本的额外字段"},
            "history": ["ignored"],
        })
        restarted = self._store(tmp_path)
        assert restarted.degraded is True and restarted.degraded_reason == "main_corrupt"
        # 降级期间不加载记录, 额外键不影响判定; recover 校验通过后才恢复读取
        assert restarted.lookup_request("r1", "bot") is None
        assert restarted.recover() is True
        assert restarted.degraded is False
        assert restarted.lookup_request("r1", "bot") is not None
        rewritten = json.loads((tmp_path / "store.manifest.json").read_text(encoding="utf-8"))
        assert rewritten["degraded"] is None and "history" not in rewritten

    def test_store_manifest_empty_reason_is_unreadable(self, tmp_path: Path):
        store = self._store(tmp_path)
        store.accept_request("bot", "r1", "fp", "group:20", "op")
        self._write_manifest(tmp_path, {
            "version": 1, "initialized_at": 1.0, "updated_at": 2.0,
            "expected_files": {"main": "present", "archive": "absent"},
            "degraded": {"reason": "", "at": 3.0},
        })
        # 不可读清单按"有数据文件"迁移, 不静默当空库, 也不报告降级
        restarted = self._store(tmp_path)
        assert restarted.degraded is False
        assert restarted.lookup_request("r1", "bot") is not None
        rewritten = json.loads((tmp_path / "store.manifest.json").read_text(encoding="utf-8"))
        assert rewritten["degraded"] is None
        assert "history" not in rewritten

    def test_store_manifest_contradiction_is_unreadable(self, tmp_path: Path):
        store = self._store(tmp_path)
        store.accept_request("bot", "r1", "fp", "group:20", "op")
        # 未降级却声明主文件缺失: 自相矛盾, 按不可读迁移而不是接受
        self._write_manifest(tmp_path, {
            "version": 1, "initialized_at": 1.0, "updated_at": 2.0,
            "expected_files": {"main": "missing", "archive": "absent"}, "degraded": None,
        })
        restarted = self._store(tmp_path)
        assert restarted.degraded is False
        assert restarted.lookup_request("r1", "bot") is not None
        rewritten = json.loads((tmp_path / "store.manifest.json").read_text(encoding="utf-8"))
        assert rewritten["expected_files"] == {"main": "present", "archive": "absent"}

    def test_store_marker_failure_still_degrades_in_memory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        path = tmp_path / "store.json"
        path.write_text("not-json{", encoding="utf-8")

        def broken_write(self: ManualWakeStore, manifest: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(ManualWakeStore, "_write_manifest", broken_write)
        store = self._store(tmp_path)
        assert store.degraded is True
        assert store.degraded_reason == "manifest_missing_with_data"
        assert path.read_text(encoding="utf-8") == "not-json{"
        assert list(tmp_path.glob("store.json.corrupt-*")) == []

    def test_store_quarantine_failure_keeps_degraded_after_restart(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        path = tmp_path / "store.json"
        path.write_text("not-json{", encoding="utf-8")

        def broken_quarantine(item: Path) -> Path:
            raise OSError("rename failed")

        monkeypatch.setattr(durability_module, "quarantine_file", broken_quarantine)
        store = self._store(tmp_path)
        assert store.degraded is True
        # 隔离失败不解除降级: 重启按清单与坏文件重新判定
        monkeypatch.undo()
        restarted = self._store(tmp_path)
        assert restarted.degraded is True and restarted.degraded_reason != ""

    def test_ledger_samples(self, tmp_path: Path):
        ledger = RequestApprovalLedger(tmp_path / "request_ledger.json")
        assert ledger.degraded is False
        # 额外键与空降级原因: 账本保留额外键, 空原因回落为 degraded
        (tmp_path / "request_ledger.manifest.json").write_text(json.dumps({
            "version": 1, "initialized_at": 1.0, "updated_at": 2.0,
            "expected_files": {"entries": "present"},
            "degraded": {"reason": "", "at": "raw", "note": "旧字段"},
            "history": ["kept"],
        }, ensure_ascii=False), encoding="utf-8")
        restarted = RequestApprovalLedger(tmp_path / "request_ledger.json")
        assert restarted.degraded is True and restarted.degraded_reason == "degraded"
        # 清单状态与数据文件不一致: 未降级清单要求 entries 恒存在, 其它取值按清单损坏迁移
        (tmp_path / "request_ledger.manifest.json").write_text(json.dumps({
            "version": 1, "initialized_at": 1.0, "updated_at": 2.0,
            "expected_files": {"entries": "missing"}, "degraded": None,
        }, ensure_ascii=False), encoding="utf-8")
        migrated = RequestApprovalLedger(tmp_path / "request_ledger.json")
        assert migrated.degraded is False
        assert json.loads((tmp_path / "request_ledger.manifest.json").read_text(encoding="utf-8"))["expected_files"] == {"entries": "present"}

    def test_ledger_degrade_write_keeps_manifest_extras(self, tmp_path: Path):
        """真实降级写入不得丢掉旧清单的 expected_files 与顶层扩展键"""
        ledger = RequestApprovalLedger(tmp_path / "request_ledger.json")
        assert ledger.degraded is False
        (tmp_path / "request_ledger.manifest.json").write_text(json.dumps({
            "version": 1, "initialized_at": 1.0, "updated_at": 2.0,
            "expected_files": {"entries": "present", "future": "keep"},
            "degraded": None, "history": ["kept"],
        }, ensure_ascii=False), encoding="utf-8")
        restarted = RequestApprovalLedger(tmp_path / "request_ledger.json")
        assert restarted.degraded is False
        assert restarted._mark_degraded("probe", "审计反例: 降级写入") is True
        rewritten = json.loads((tmp_path / "request_ledger.manifest.json").read_text(encoding="utf-8"))
        assert rewritten["expected_files"] == {"entries": "present", "future": "keep"}
        assert rewritten["history"] == ["kept"]
        # 标记是本次新写入的: 按声明字段生成, 带真实时间
        assert rewritten["degraded"]["reason"] == "probe"
        assert rewritten["degraded"]["at"] > 0

    def test_ledger_recover_failure_keeps_degraded(self, tmp_path: Path):
        path = tmp_path / "request_ledger.json"
        path.write_text("{ 不是 JSON", encoding="utf-8")
        ledger = RequestApprovalLedger(path)
        assert ledger.degraded is True
        assert ledger.recover() is False
        assert ledger.degraded is True and ledger.degraded_reason == "manifest_missing_with_data"

    def test_memory_ledger_creates_no_files(self, tmp_path: Path):
        ledger = RequestApprovalLedger(None)
        ledger.degraded = True
        assert ledger._mark_degraded("init_failed", "内存账本") is True
        assert ledger.degraded is True and ledger.degraded_reason == "init_failed"
        # 无持久路径的账本不写任何文件
        assert list(tmp_path.iterdir()) == []
