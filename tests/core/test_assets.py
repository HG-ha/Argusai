from argusai.core.workspace.assets import normalize_asset_ref


def test_normalize_prefixed_asset_ref() -> None:
    asset = normalize_asset_ref("apk:C:/apps/demo.apk")

    assert asset.type == "apk"
    assert asset.value == "C:/apps/demo.apk"
    assert asset.to_ref() == "apk:C:/apps/demo.apk"


def test_normalize_dict_asset_ref_for_binary() -> None:
    asset = normalize_asset_ref({
        "type": "exe",
        "value": "sample.exe",
        "label": "Windows sample",
        "scope": ["local-only"],
        "metadata": {"sha256": "abc"},
    })

    assert asset.to_dict() == {
        "type": "exe",
        "value": "sample.exe",
        "label": "Windows sample",
        "scope": ["local-only"],
        "metadata": {"sha256": "abc"},
    }


def test_unknown_asset_type_falls_back_to_other() -> None:
    asset = normalize_asset_ref("weird:thing")

    assert asset.type == "other"
    assert asset.value == "thing"
    assert asset.to_ref() == "other:thing"

