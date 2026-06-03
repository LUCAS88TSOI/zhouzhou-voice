"""贊助/捐款連結回歸測試。

最關鍵的風險：PayMe 連結若打錯一個字元，用戶捐的款會過數到別人帳戶（或失敗）。
這裡用精確比對鎖死正確連結，並驗證 app 內按鈕指向官網贊助區。
"""

from pathlib import Path

import pytest

from gui import settings_panel


# 唯一正確的收款連結（HSBC PayMe 個人 PayCode 短連結）。
# 任何改動都應為「有意」的，否則此測試會擋下。
EXPECTED_PAYME_URL = "https://payme.hsbc/289b982f31514bdfafa7d3e597aa1ab2"


def test_payme_url_is_exact() -> None:
    """PayMe 收款連結必須一字不差。"""
    assert settings_panel.PAYME_URL == EXPECTED_PAYME_URL


def test_donate_url_points_to_official_repo_support_section() -> None:
    """App 捐款按鈕應跳轉到官網（公開倉）的贊助段落 anchor。"""
    assert settings_panel.DONATE_URL.startswith(
        "https://github.com/LUCAS88TSOI/zhouzhou-voice"
    )
    assert settings_panel.DONATE_URL.endswith("#贊助支持")


def test_project_url_is_real_not_placeholder() -> None:
    """專案連結不可再殘留 your-org 佔位符。"""
    assert "your-org" not in settings_panel.PROJECT_URL
    assert settings_panel.PROJECT_URL == "https://github.com/LUCAS88TSOI/zhouzhou-voice"


def test_readme_payme_link_matches_constant() -> None:
    """README 內嵌的 PayMe 連結必須與 PAYME_URL 一致（防兩處分叉，過數錯人）。"""
    readme = Path(__file__).resolve().parent.parent / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert settings_panel.PAYME_URL in text, "README 找不到正確的 PayMe 連結"


def test_payme_qr_asset_exists() -> None:
    """README/關於頁引用的 PayMe QR 圖檔必須存在於 assets。"""
    qr = Path(__file__).resolve().parent.parent / "assets" / "payme-qr.jpg"
    assert qr.is_file(), f"找不到 PayMe QR 圖檔：{qr}"
    assert qr.stat().st_size > 1024, "QR 圖檔過小，可能損毀"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
